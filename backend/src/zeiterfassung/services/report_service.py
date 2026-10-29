"""Admin dashboard, flagged timesheets, bulk approval, reports, audit log.

Moved verbatim out of the former monolithic ``api/v1/admin.py``
(zeiterfassung-6pcv). Permission checks that need the resolved ``AuthUser``
object (incl. ``get_scope()``) stay in the router; everything DB-query-backed
and the report-building logic lives here.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from zeiterfassung.db.scoping import get_scoped, tenant_scoped
from zeiterfassung.models.absence import ApprovalRequest, RequestStatus, RequestType
from zeiterfassung.models.audit import AuditLog
from zeiterfassung.models.clock import DailyTimesheet, TimesheetStatus
from zeiterfassung.policy.tagesregeln import Tagesregeln
from zeiterfassung.schemas.absence import AbsenceRequestResponse
from zeiterfassung.schemas.admin_reports import (
    ApprovalContext,
    AuditLogEntry,
    DashboardStats,
    EmployeeMonthSummary,
    EmployeeYearReport,
    MonthlyTimesheet,
    MonthReconciliation,
    OverlappingAbsence,
    ReconciliationLohnart,
    ReconciliationStunden,
    ReconciliationUeberstunden,
    ReconciliationZuschlaege,
    TimesheetDay,
    TimesheetSegment,
    TimesheetSummary,
    YearAbsenceEntry,
    YearMonthSummary,
)
from zeiterfassung.schemas.clock import DailyTimesheetResponse
from zeiterfassung.services import absence_service

_WEEKDAY_LABELS = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")


async def approval_context(
    db: AsyncSession, tenant_id: uuid.UUID, request_id: uuid.UUID
) -> ApprovalContext:
    """Return enriched context for a leave request to help approvers make informed decisions."""
    from zeiterfassung.models.user import User

    # Load the target request
    try:
        req = await absence_service.get_request(db, tenant_id, request_id)
    except absence_service.RequestNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Request not found"
        ) from exc

    # Enrich request with employee name
    requester_result = await db.execute(select(User).where(User.id == req.employee_id))
    requester = requester_result.scalar_one_or_none()
    request_response = AbsenceRequestResponse.model_validate(req)
    if requester:
        request_response.employee_name = f"{requester.first_name} {requester.last_name}"
        request_response.employee_number = requester.employee_number

    # Vacation balance for the year of the request's start date
    vacation_balance = await absence_service.get_vacation_balance(
        db, tenant_id, req.employee_id, req.date_from.year
    )

    # All other approved/submitted absences overlapping the request's date range
    overlapping_result = await db.execute(
        tenant_scoped(select(ApprovalRequest), ApprovalRequest, tenant_id).where(
            ApprovalRequest.id != request_id,
            ApprovalRequest.date_from <= req.date_to,
            ApprovalRequest.date_to >= req.date_from,
            ApprovalRequest.status.in_([RequestStatus.APPROVED, RequestStatus.SUBMITTED]),
        )
    )
    overlapping_requests = overlapping_result.scalars().all()

    # Fetch employee names for all overlapping requests
    overlapping_employee_ids = {r.employee_id for r in overlapping_requests}
    if overlapping_employee_ids:
        users_result = await db.execute(select(User).where(User.id.in_(overlapping_employee_ids)))
        overlap_users = {u.id: u for u in users_result.scalars().all()}
    else:
        overlap_users = {}

    overlapping_absences: list[OverlappingAbsence] = []
    for r in overlapping_requests:
        u = overlap_users.get(r.employee_id)
        overlapping_absences.append(
            OverlappingAbsence(
                employee_name=f"{u.first_name} {u.last_name}" if u else str(r.employee_id),
                employee_number=u.employee_number if u else None,
                request_type=r.request_type.value,
                status=r.status.value,
                date_from=r.date_from,
                date_to=r.date_to,
                days_count=float(r.days_count),
            )
        )

    # Total active employees in the tenant
    active_count_result = await db.execute(
        tenant_scoped(select(func.count()).select_from(User), User, tenant_id).where(
            User.is_active == True,  # noqa: E712
        )
    )
    total_active = active_count_result.scalar_one()

    # Absent during period = employees with APPROVED overlapping absences + this request if approved
    approved_employee_ids = {
        r.employee_id for r in overlapping_requests if r.status == RequestStatus.APPROVED
    }
    # Include the requester as absent (assuming this request gets approved)
    approved_employee_ids.add(req.employee_id)
    absent_during_period = len(approved_employee_ids)

    # Staffing percentage and warning
    if total_active > 0:
        staffing_percentage = (total_active - absent_during_period) / total_active * 100
    else:
        staffing_percentage = 100.0

    staffing_warning: str | None = None
    if staffing_percentage < 75.0:
        staffing_warning = f"Kritisch: nur {staffing_percentage:.0f}% Besetzung"

    return ApprovalContext(
        request=request_response,
        vacation_balance=vacation_balance,
        overlapping_absences=overlapping_absences,
        total_active_employees=total_active,
        absent_during_period=absent_during_period,
        staffing_percentage=round(staffing_percentage, 1),
        staffing_warning=staffing_warning,
    )


async def dashboard_stats(db: AsyncSession, tenant_id: uuid.UUID) -> DashboardStats:
    """Return high-level dashboard statistics."""
    from zeiterfassung.models.export import ExportStatus, PayrollExport
    from zeiterfassung.models.user import User

    today = date.today()
    today.replace(day=1)

    # Active users
    users_result = await db.execute(
        tenant_scoped(select(func.count()).select_from(User), User, tenant_id).where(
            User.is_active == True,  # noqa: E712
        )
    )
    total_active = users_result.scalar_one()

    # Flagged timesheets
    flagged_result = await db.execute(
        tenant_scoped(
            select(func.count()).select_from(DailyTimesheet), DailyTimesheet, tenant_id
        ).where(
            DailyTimesheet.status == TimesheetStatus.FLAGGED,
        )
    )
    flagged_count = flagged_result.scalar_one()

    # Pending approvals
    pending_result = await db.execute(
        tenant_scoped(
            select(func.count()).select_from(ApprovalRequest), ApprovalRequest, tenant_id
        ).where(
            ApprovalRequest.status == RequestStatus.SUBMITTED,
        )
    )
    pending_count = pending_result.scalar_one()

    # Exports this month
    exports_result = await db.execute(
        tenant_scoped(
            select(func.count()).select_from(PayrollExport), PayrollExport, tenant_id
        ).where(
            PayrollExport.created_at >= datetime(today.year, today.month, 1, tzinfo=UTC),
            PayrollExport.status == ExportStatus.GENERATED,
        )
    )
    exports_count = exports_result.scalar_one()

    return DashboardStats(
        total_active_users=total_active,
        timesheets_flagged=flagged_count,
        pending_approvals=pending_count,
        exports_this_month=exports_count,
    )


async def flagged_timesheets(
    db: AsyncSession, tenant_id: uuid.UUID, skip: int, limit: int
) -> list[DailyTimesheetResponse]:
    """List all flagged timesheets requiring admin attention."""
    result = await db.execute(
        tenant_scoped(select(DailyTimesheet), DailyTimesheet, tenant_id)
        .where(
            DailyTimesheet.status == TimesheetStatus.FLAGGED,
        )
        .order_by(DailyTimesheet.work_date.desc())
        .offset(skip)
        .limit(limit)
    )
    timesheets = result.scalars().all()
    return [DailyTimesheetResponse.model_validate(t) for t in timesheets]


async def pending_approvals(
    db: AsyncSession, tenant_id: uuid.UUID, scope, skip: int, limit: int
) -> list[AbsenceRequestResponse]:
    """List pending leave approval requests with employee names.

    ``scope`` is the caller's ``AuthUser.get_scope()`` result (resolved by the
    router) so Teamleiter sees only their department's pending requests.
    """
    from zeiterfassung.models.user import User

    requests = await absence_service.list_requests(
        db,
        tenant_id,
        status=RequestStatus.SUBMITTED,
        skip=skip,
        limit=limit,
        scope=scope,
    )

    # Enrich with employee names
    employee_ids = {r.employee_id for r in requests}
    if employee_ids:
        users_result = await db.execute(select(User).where(User.id.in_(employee_ids)))
        users = {u.id: u for u in users_result.scalars().all()}
    else:
        users = {}

    results = []
    for r in requests:
        data = AbsenceRequestResponse.model_validate(r)
        u = users.get(r.employee_id)
        if u:
            data.employee_name = f"{u.first_name} {u.last_name}"
            data.employee_number = u.employee_number
        results.append(data)
    return results


async def bulk_approve(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    request_ids: list[uuid.UUID],
    actor_id: uuid.UUID,
    reason: str | None,
) -> list[AbsenceRequestResponse]:
    """Approve multiple leave requests in one call."""
    results = []
    errors = []

    for req_id in request_ids:
        try:
            req = await absence_service.approve_request(db, tenant_id, req_id, actor_id, reason)
            results.append(AbsenceRequestResponse.model_validate(req))
        except (
            absence_service.RequestNotFoundError,
            absence_service.InvalidStatusTransitionError,
            # Bugfix: Self-Approval (4-Augen) auch beim Bulk überspringen statt 500.
            absence_service.SelfApprovalError,
        ) as exc:
            errors.append(f"{req_id}: {exc}")

    if errors and not results:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=errors,
        )

    await db.commit()
    return results


async def monthly_report(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    year: int,
    month: int,
    *,
    has_view_all: bool,
    has_view_team: bool,
    caller_department_id: uuid.UUID | None,
    caller_department_ids: list[uuid.UUID] | None,
) -> list[EmployeeMonthSummary]:
    """Per-employee summary for a given month (for admin reports page)."""
    from zeiterfassung.models.user import User

    month_start = date(year, month, 1)
    if month == 12:
        month_end = date(year + 1, 1, 1)
    else:
        month_end = date(year, month + 1, 1)

    # Get employees — department-scoped for Teamleiter
    users_stmt = tenant_scoped(select(User), User, tenant_id).where(
        User.is_active == True,  # noqa: E712
    )
    if has_view_team and not has_view_all and caller_department_id is not None:
        users_stmt = users_stmt.where(
            User.department_id.in_(caller_department_ids or [caller_department_id])
        )
    users_result = await db.execute(users_stmt)
    users = {u.id: u for u in users_result.scalars().all()}

    # Get timesheets for the month
    ts_result = await db.execute(
        tenant_scoped(select(DailyTimesheet), DailyTimesheet, tenant_id).where(
            DailyTimesheet.work_date >= month_start,
            DailyTimesheet.work_date < month_end,
        )
    )
    timesheets = ts_result.scalars().all()

    # Get absence requests (approved) for the month
    abs_result = await db.execute(
        tenant_scoped(select(ApprovalRequest), ApprovalRequest, tenant_id).where(
            ApprovalRequest.status == RequestStatus.APPROVED,
            ApprovalRequest.date_from < month_end,
            ApprovalRequest.date_to >= month_start,
        )
    )
    absences = abs_result.scalars().all()

    # Aggregate per employee
    summaries: dict[uuid.UUID, dict] = {}
    for u in users.values():
        summaries[u.id] = {
            "employee_id": u.id,
            "employee_name": f"{u.first_name} {u.last_name}",
            "employee_number": u.employee_number,
            "days_worked": 0,
            "total_work_minutes": 0,
            "total_overtime_minutes": 0,
            "total_break_minutes": 0,
            "days_vacation": 0,
            "days_sick": 0,
            "night_supplement_minutes": 0,
            "sunday_supplement_minutes": 0,
            "holiday_supplement_minutes": 0,
        }

    for ts in timesheets:
        s = summaries.get(ts.employee_id)
        if not s:
            continue
        if ts.net_work_minutes > 0:
            s["days_worked"] += 1
        s["total_work_minutes"] += ts.net_work_minutes
        s["total_break_minutes"] += ts.break_minutes
        if ts.is_night_work:
            s["night_supplement_minutes"] += ts.net_work_minutes
        if ts.is_sunday_work:
            s["sunday_supplement_minutes"] += ts.net_work_minutes
        if ts.is_holiday_work:
            s["holiday_supplement_minutes"] += ts.net_work_minutes

    from zeiterfassung.core.holidays import working_days_between
    from zeiterfassung.services.tenant_service import resolve_bundesland

    bundesland = await resolve_bundesland(db, tenant_id)
    last_day = month_end - timedelta(days=1)  # month_end ist exklusiv (s. Queries oben)
    for ab in absences:
        s = summaries.get(ab.employee_id)
        if not s:
            continue
        # Halbtage zaehlen 0,5; Antraege werden auf den Monat geclippt (#74).
        frac = Tagesregeln.abwesenheits_deckung(ab)
        frm = max(ab.date_from, month_start)
        to = min(ab.date_to, last_day)
        if frm > to:
            continue
        days = working_days_between(frm, to, bundesland) * frac
        if ab.request_type.value == "VACATION":
            s["days_vacation"] += days
        elif ab.request_type.value == "SICK_LEAVE":
            s["days_sick"] += days

    # Monatssaldo (Ist − Soll, Plus UND Minus verrechnet) aus der Konto-
    # Rechnung — dieselbe Zahl wie Stundenzettel, Kalenderkopf und Abgleich.
    # Frueher stand hier die Summe der Plus-Tage (ts.overtime_minutes), die
    # Minus-Tage ignorierte und damit von allen anderen Anzeigen abwich.
    from zeiterfassung.services import overtime_service

    for u in users.values():
        summaries[u.id]["total_overtime_minutes"] = (
            await overtime_service.month_summary(db, tenant_id, u, year, month)
        )["saldo_minutes"]

    return [EmployeeMonthSummary(**s) for s in summaries.values()]


async def yearly_report(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    year: int,
    employee_id: uuid.UUID,
    *,
    has_view_all: bool,
    has_view_team: bool,
    caller_department_id: uuid.UUID | None,
    caller_department_ids: list[uuid.UUID] | None,
) -> EmployeeYearReport:
    """Jahresauswertung fuer EINEN Mitarbeiter: 12 Monatszeilen + Abwesenheiten."""
    from zeiterfassung.core.holidays import working_days_between
    from zeiterfassung.models.user import User
    from zeiterfassung.services.tenant_service import resolve_bundesland

    user = await get_scoped(db, User, tenant_id, employee_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")
    if has_view_team and not has_view_all:
        allowed = set(
            caller_department_ids or ([caller_department_id] if caller_department_id else [])
        )
        if user.department_id not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Employee outside your department"
            )

    year_start = date(year, 1, 1)
    year_end = date(year, 12, 31)
    bundesland = await resolve_bundesland(db, tenant_id)

    months = [
        {
            "month": m,
            "days_worked": 0,
            "work_minutes": 0,
            "overtime_minutes": 0,
            "days_vacation": 0.0,
            "days_sick": 0.0,
            "days_other_absence": 0.0,
        }
        for m in range(1, 13)
    ]

    ts_rows = (
        (
            await db.execute(
                tenant_scoped(select(DailyTimesheet), DailyTimesheet, tenant_id).where(
                    DailyTimesheet.employee_id == employee_id,
                    DailyTimesheet.work_date >= year_start,
                    DailyTimesheet.work_date <= year_end,
                )
            )
        )
        .scalars()
        .all()
    )
    for ts in ts_rows:
        s = months[ts.work_date.month - 1]
        if ts.net_work_minutes > 0:
            s["days_worked"] += 1
        s["work_minutes"] += ts.net_work_minutes

    # Monatssaldo je Monat aus der Konto-Rechnung (s. monthly_report):
    # Zukunftsmonate liefern 0, der laufende Monat "bis heute".
    from zeiterfassung.services import overtime_service

    for s in months:
        s["overtime_minutes"] = (
            await overtime_service.month_summary(db, tenant_id, user, year, s["month"])
        )["saldo_minutes"]

    abs_rows = (
        (
            await db.execute(
                tenant_scoped(select(ApprovalRequest), ApprovalRequest, tenant_id)
                .where(
                    ApprovalRequest.employee_id == employee_id,
                    ApprovalRequest.status == RequestStatus.APPROVED,
                    ApprovalRequest.date_from <= year_end,
                    ApprovalRequest.date_to >= year_start,
                    ApprovalRequest.request_type != RequestType.CORRECTION,
                )
                .order_by(ApprovalRequest.date_from)
            )
        )
        .scalars()
        .all()
    )
    absences: list[YearAbsenceEntry] = []
    for ab in abs_rows:
        frac = Tagesregeln.abwesenheits_deckung(ab)
        rtype = ab.request_type.value if hasattr(ab.request_type, "value") else str(ab.request_type)
        key = (
            "days_vacation"
            if rtype == "VACATION"
            else "days_sick"
            if rtype == "SICK_LEAVE"
            else "days_other_absence"
        )
        # Pro Monat clippen und anteilig zaehlen.
        for m in range(1, 13):
            m_start = date(year, m, 1)
            m_end = date(year, m, 28) + timedelta(days=4)
            m_end = m_end - timedelta(days=m_end.day)
            frm = max(ab.date_from, m_start)
            to = min(ab.date_to, m_end)
            if frm > to:
                continue
            months[m - 1][key] += working_days_between(frm, to, bundesland) * frac
        absences.append(
            YearAbsenceEntry(
                id=ab.id,
                request_type=rtype,
                date_from=ab.date_from,
                date_to=ab.date_to,
                day_part=(
                    ab.day_part.value
                    if getattr(ab, "day_part", None) is not None and hasattr(ab.day_part, "value")
                    else (str(ab.day_part) if getattr(ab, "day_part", None) else None)
                ),
                days_count=float(ab.days_count or 0.0),
                sick_status=(str(ab.sick_status) if ab.sick_status else None),
            )
        )

    return EmployeeYearReport(
        employee_id=user.id,
        employee_name=f"{user.first_name} {user.last_name}",
        employee_number=user.employee_number,
        year=year,
        months=[YearMonthSummary(**m) for m in months],
        absences=absences,
    )


def _segment_out(
    lo: datetime,
    hi: datetime,
    punched: tuple[datetime, datetime] | None,
    day_end_local: datetime,
) -> TimesheetSegment:
    """Ein Segment fuer den Stundenzettel — mit Rohstempeln, falls gekappt wurde.

    ``punched`` sind die UNGEKAPPTEN lokalen Stempelzeiten (Start, Ende) des
    zugehoerigen Segments. Der Tages-Schnitt um Mitternacht (``hi ==
    day_end_local``) ist KEINE Fenster-Kappung und wird nicht markiert.
    """
    punched_start = punched[0] if punched else None
    punched_end = punched[1] if punched else None
    start_gekappt = punched_start is not None and punched_start < lo
    ende_gekappt = punched_end is not None and punched_end > hi and hi < day_end_local
    return TimesheetSegment(
        start=f"{lo:%H:%M}",
        end=f"{hi:%H:%M}",
        start_punched=f"{punched_start:%H:%M}" if start_gekappt else None,
        clipped_earliest_start=start_gekappt,
        end_punched=f"{punched_end:%H:%M}" if ende_gekappt else None,
        clipped_window_end=ende_gekappt,
    )


async def monthly_timesheet(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    year: int,
    month: int,
    employee_id: uuid.UUID,
    *,
    has_view_all: bool,
    has_view_team: bool,
    caller_department_id: uuid.UUID | None,
    caller_department_ids: list[uuid.UUID] | None,
) -> MonthlyTimesheet:
    """Monats-Stundenzettel fuer EINEN Mitarbeiter (wie das alte Altsystem-Blatt).

    Pro Kalendertag: Kommt/Geht-Segmente (lokale Zeit Europe/Berlin), Pause,
    Ist, Soll, Differenz und Abwesenheit. Berechnung analog zum Soll-Modus des
    DATEV-Exports (services/export_service._build_datev_rows): Ist minutengenau
    aus den Stempel-Events (Korrekturen/Voids gefiltert), Soll aus dem
    Wochentagsplan bzw. den Vertragsstunden; Feiertage und ganztaegige
    Abwesenheiten haben kein Tagessoll (diff = 0 ohne Arbeit).
    """
    from zoneinfo import ZoneInfo

    from zeiterfassung.core.holidays import is_holiday
    from zeiterfassung.models.clock import PunchEvent
    from zeiterfassung.models.tenant import Tenant
    from zeiterfassung.models.user import User
    from zeiterfassung.models.work_time_profile import WorkTimeProfile
    from zeiterfassung.payroll.rules import (
        WorkSegment,
        clip_segments_window_local_tz,
        parse_earliest_start,
        segments_from_events,
    )
    from zeiterfassung.policy import worktime
    from zeiterfassung.services.tenant_service import resolve_bundesland

    user = await get_scoped(db, User, tenant_id, employee_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")
    if has_view_team and not has_view_all:
        allowed = set(
            caller_department_ids or ([caller_department_id] if caller_department_id else [])
        )
        if user.department_id not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Employee outside your department"
            )

    month_start = date(year, month, 1)
    if month == 12:
        month_end = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        month_end = date(year, month + 1, 1) - timedelta(days=1)
    bundesland = await resolve_bundesland(db, tenant_id)
    tzinfo = ZoneInfo("Europe/Berlin")

    # ── Stempel-Events des Monats (±1 Tag Rand fuer lokale Tageszuordnung) ──
    win_start = datetime(
        month_start.year, month_start.month, month_start.day, tzinfo=UTC
    ) - timedelta(days=1)
    win_end = datetime(
        month_end.year, month_end.month, month_end.day, 23, 59, 59, tzinfo=UTC
    ) + timedelta(days=1)
    superseded = set(
        (
            await db.execute(
                tenant_scoped(select(PunchEvent.corrects_event_id), PunchEvent, tenant_id).where(
                    PunchEvent.corrects_event_id.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )
    ev_rows = (
        (
            await db.execute(
                tenant_scoped(select(PunchEvent), PunchEvent, tenant_id)
                .where(
                    PunchEvent.employee_id == employee_id,
                    PunchEvent.event_time >= win_start,
                    PunchEvent.event_time <= win_end,
                    PunchEvent.is_void.is_(False),
                )
                .order_by(PunchEvent.event_time, PunchEvent.id)
            )
        )
        .scalars()
        .all()
    )
    events: list[tuple[str, datetime]] = []
    for ev in ev_rows:
        if ev.id in superseded:
            continue
        et = ev.event_time
        if et.tzinfo is not None:
            et = et.astimezone(UTC).replace(tzinfo=None)
        etype = ev.event_type.value if hasattr(ev.event_type, "value") else str(ev.event_type)
        events.append((etype, et))
    segments = segments_from_events(events)

    # Stempel-Kappung: dieselbe Fallback-Kette wie Konto (clock_service) und
    # DATEV-Export — aktives NFC-Arbeitszeit-Profil (Fenster mit Beginn UND
    # Ende, ggf. ueber Mitternacht), sonst Lohnprofil-Regel "fruehester_beginn"
    # (nur Beginn), sonst keine Kappung.
    tenant_row = (
        await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    ).scalar_one_or_none()
    wt_profile = (
        await get_scoped(db, WorkTimeProfile, tenant_id, user.work_time_profile_id)
        if user.work_time_profile_id
        else None
    )
    window = worktime.nfc_window(wt_profile)
    if window is None:
        profiles = {
            str(p.get("id")): p
            for p in (
                ((tenant_row.settings or {}).get("wage_profiles") or []) if tenant_row else []
            )
        }
        profile = profiles.get(user.wage_profile_id) if user.wage_profile_id else None
        earliest_min = parse_earliest_start(profile.get("fruehester_beginn")) if profile else None
        if earliest_min is not None:
            window = worktime.Window(start_min=earliest_min, end_min=None)
    # Ungekappte Segmente behalten: der Zettel soll beide Zeiten zeigen
    # ("gestempelt 05:55, gewertet ab 06:00"). Paarweise kappen, damit jedes
    # gewertete Segment seinem Rohstempel zugeordnet bleibt.
    seg_pairs: list[tuple[WorkSegment, WorkSegment]] = []
    for seg in segments:
        for clipped in clip_segments_window_local_tz([seg], window, tzinfo):
            seg_pairs.append((clipped, seg))
    segments = [clipped for clipped, _ in seg_pairs]

    # ── Tagessoll: Wochentagsplan des Users, sonst Vertragsstunden Mo–Fr ──

    # Tages-Soll: EINE Stelle fuer alle Konsumenten (policy.tagesregeln) —
    # inkl. Arbeitszeit-Profil, das der Zettel frueher nicht kannte (die
    # _tagessoll-Kopie hier uebersprang den Profil-Vorrang, Spec 25.08.).
    _regeln = Tagesregeln.fuer(user, wt_profile)
    _tagessoll = _regeln.tagessoll

    # ── Genehmigte Abwesenheiten (Urlaub/Krank), auf den Monat geclippt ──
    absences = (
        (
            await db.execute(
                tenant_scoped(select(ApprovalRequest), ApprovalRequest, tenant_id).where(
                    ApprovalRequest.employee_id == employee_id,
                    ApprovalRequest.status == RequestStatus.APPROVED,
                    ApprovalRequest.date_from <= month_end,
                    ApprovalRequest.date_to >= month_start,
                    ApprovalRequest.request_type.in_(
                        [
                            RequestType.VACATION,
                            RequestType.SICK_LEAVE,
                            RequestType.SPECIAL_LEAVE,
                            RequestType.UNPAID_LEAVE,
                            RequestType.OVERTIME_REDUCTION,
                        ]
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    absent_frac: dict[date, float] = {}
    absence_label: dict[date, str] = {}
    _LABELS = {
        RequestType.VACATION: "Urlaub",
        RequestType.SICK_LEAVE: "Krank",
        RequestType.SPECIAL_LEAVE: "Sonderurlaub",
        RequestType.UNPAID_LEAVE: "Unbezahlt",
        RequestType.OVERTIME_REDUCTION: "Überst.-Abbau",
    }
    for ab in absences:
        frac = Tagesregeln.abwesenheits_deckung(ab)
        label = _LABELS.get(ab.request_type, str(ab.request_type))
        dd = max(ab.date_from, month_start)
        to = min(ab.date_to, month_end)
        while dd <= to:
            # Überstundenabbau deckt das Soll NICHT — der Tag zeigt sein Minus
            # (genau das ist der Abbau); nur das Label wird gesetzt.
            if ab.request_type != RequestType.OVERTIME_REDUCTION:
                absent_frac[dd] = min(1.0, absent_frac.get(dd, 0.0) + frac)
            absence_label.setdefault(dd, label)
            dd += timedelta(days=1)

    # ── Tagesboegen des Monats: Fallback fuer Tage OHNE Stempel-Events ──
    # Auto-Erfasser und manuell gesetzte Tage haben einen abgeleiteten Bogen,
    # aber keine punch_events. Ohne diesen Fallback zeigte der Zettel dort
    # 0,00 h, waehrend Tagesbogen, Konto und Export die Zeit kennen
    # (Auto-Erfasser standen mit leerem Monatszettel da). Tage MIT Stempeln
    # rechnen unveraendert aus den Events — der Bogen wird nur gelesen, wenn
    # der Tag sonst keinerlei gewertetes Segment hat.
    ts_by_day: dict[date, DailyTimesheet] = {
        t.work_date: t
        for t in (
            await db.execute(
                tenant_scoped(select(DailyTimesheet), DailyTimesheet, tenant_id).where(
                    DailyTimesheet.employee_id == employee_id,
                    DailyTimesheet.work_date >= month_start,
                    DailyTimesheet.work_date <= month_end,
                )
            )
        )
        .scalars()
        .all()
    }

    days_out: list[TimesheetDay] = []
    sum_soll = 0
    sum_ist = 0
    urlaub_min = 0
    urlaub_tage = 0.0
    krank_min = 0
    krank_tage = 0.0
    brutto_soll = 0
    feiertag_min = 0
    feiertag_tage = 0.0
    sonstige_min = 0
    sonstige_tage = 0.0

    day = month_start
    while day <= month_end:
        holiday = is_holiday(day, bundesland)
        # Lokaler UTC-Offset des Tages (Mittag als DST-sichere Referenz).
        off = int(
            datetime(day.year, day.month, day.day, 12, tzinfo=tzinfo).utcoffset().total_seconds()
            // 60
        )
        offset = timedelta(minutes=off)

        # ── EINE Quelle: Ist und Pause kommen aus dem Tagesbogen ─────────────
        # (clock_service rechnet ihn aus den Stempeln — sekundengenau, mit
        # ArbZG-Mindestpause, Kappung und Sonderregeln wie "Ausstempeln
        # vergessen" = 0 h). Der Zettel rechnet NICHT mehr parallel: Kalender,
        # Tagesdetail, Konto und Zettel zeigen damit konstruktionsbedingt
        # dieselbe Zahl. Die Stempel unten dienen nur noch der Anzeige
        # (Kommt/Geht-Segmente, gestempelt vs. gewertet).
        ts_row = ts_by_day.get(day)
        ist = int(ts_row.net_work_minutes or 0) if ts_row is not None else 0
        pause = int(ts_row.break_minutes or 0) if ts_row is not None else 0
        flagged = ts_row.flagged_reason if ts_row is not None else None
        # Die ArbZG-§4-Pausenwarnung gehoert nicht auf den Zettel: dort steht
        # die angehobene Pause schon als "0:45 (0:00)§" mit Fussnote. Nur
        # Hinweise, die den Tageswert erklaeren (z. B. "Ausstempeln
        # vergessen" = 0 h, "Gehen fehlt"), bleiben sichtbar.
        if flagged:
            rest = [
                teil
                for teil in flagged.split("; ")
                if not teil.startswith("ArbZG §4: Pause zu kurz gestempelt")
            ]
            flagged = "; ".join(rest) or None

        # Segmente des lokalen Kalendertags (auf den Tag zugeschnitten, HH:MM).
        day_start_local = datetime(day.year, day.month, day.day)
        day_end_local = day_start_local + timedelta(days=1)
        day_segments: list[tuple[datetime, datetime]] = []
        for seg in segments:
            lo = max(seg.start + offset, day_start_local)
            hi = min(seg.end + offset, day_end_local)
            if hi > lo:
                day_segments.append((lo, hi))
        day_segments.sort()
        # Gestempelte Pause (Luecken zwischen den Segmenten) — reine Anzeige;
        # hebt die gesetzliche Mindestpause den Wert an, zeigt der Zettel beides
        # ("Pause 30 / gestempelt 8"), gewertet ist laengst die Bogen-Pause.
        pause_punched = 0
        for prev, nxt in zip(day_segments, day_segments[1:], strict=False):
            gap = int((nxt[0] - prev[1]).total_seconds() // 60)
            if gap > 0:
                pause_punched += gap

        # Tag ohne Stempel, aber mit Bogen-Zeiten (manuell gesetzt / Auto-
        # Erfassung): Kommt/Geht des Bogens als ein Segment anzeigen.
        if not day_segments and ts_row is not None and ts_row.net_work_minutes:
            pause_punched = pause
            if ts_row.start_time is not None and ts_row.end_time is not None:
                ts_start = ts_row.start_time
                ts_end = ts_row.end_time
                if ts_start.tzinfo is not None:
                    ts_start = ts_start.astimezone(UTC).replace(tzinfo=None)
                if ts_end.tzinfo is not None:
                    ts_end = ts_end.astimezone(UTC).replace(tzinfo=None)
                lo = max(ts_start + offset, day_start_local)
                hi = min(ts_end + offset, day_end_local)
                if hi > lo:
                    day_segments.append((lo, hi))

        # Ungekappte Stempelzeiten fuer die Anzeige "gestempelt / gewertet",
        # zugeordnet ueber das (auf den Tag geschnittene) GEWERTETE Segmentende.
        punched_by_end = {
            min(clipped.end + offset, day_end_local): (
                punched.start + offset,
                punched.end + offset,
            )
            for clipped, punched in seg_pairs
        }

        # Soll: nur Arbeitstage (Mo–Fr, kein Feiertag); Abwesenheits-Anteil des
        # Tagessolls zaehlt in Urlaub/Krank statt ins Soll (wie im Export).
        frac_abs = absent_frac.get(day, 0.0)
        if day.weekday() < 5 and holiday:
            # Feiertag auf einem Werktag: Bruttosoll faellt an, wird aber
            # komplett durch den Feiertag gedeckt (Altsystem: "0110 Feiertagslohn").
            tagessoll = _tagessoll(day)
            brutto_soll += tagessoll
            if tagessoll > 0:
                feiertag_min += tagessoll
                feiertag_tage += 1.0
            day_soll = 0
        elif day.weekday() < 5:
            tagessoll = _tagessoll(day)
            brutto_soll += tagessoll
            day_soll = int(round(tagessoll * (1.0 - frac_abs)))
            if frac_abs > 0:
                abs_min = int(round(tagessoll * frac_abs))
                if absence_label.get(day) == "Urlaub":
                    urlaub_min += abs_min
                    urlaub_tage += frac_abs
                elif absence_label.get(day) == "Krank":
                    krank_min += abs_min
                    krank_tage += frac_abs
                else:
                    # Sonderurlaub + Unbezahlt: decken das Soll, zaehlen aber in
                    # keinen Urlaub/Krank-Summenblock (Sonderurlaub = wie Arbeit,
                    # Unbezahlt = 0 Stunden ohne Verguetung) — im Abgleich als
                    # "sonstige" ausgewiesen, damit die Soll-Kette aufgeht.
                    sonstige_min += abs_min
                    sonstige_tage += frac_abs
        else:
            day_soll = 0

        diff = ist - day_soll
        sum_soll += day_soll
        sum_ist += ist
        days_out.append(
            TimesheetDay(
                date=day,
                weekday=_WEEKDAY_LABELS[day.weekday()],
                segments=[
                    _segment_out(lo, hi, punched_by_end.get(hi), day_end_local)
                    for lo, hi in day_segments
                ],
                pause_minutes=pause,
                pause_punched_minutes=pause_punched,
                pause_raised_by_law=pause > pause_punched,
                ist_minutes=ist,
                soll_minutes=day_soll,
                diff_minutes=diff,
                absence=absence_label.get(day),
                holiday=holiday,
                flagged_reason=flagged,
            )
        )
        day += timedelta(days=1)

    return MonthlyTimesheet(
        employee_id=user.id,
        employee_name=f"{user.first_name} {user.last_name}",
        employee_number=user.employee_number,
        year=year,
        month=month,
        days=days_out,
        summary=TimesheetSummary(
            soll_minutes=sum_soll,
            ist_minutes=sum_ist,
            saldo_minutes=sum_ist - sum_soll,
            urlaub_minutes=urlaub_min,
            urlaub_tage=round(urlaub_tage, 2),
            krank_minutes=krank_min,
            krank_tage=round(krank_tage, 2),
            brutto_soll_minutes=brutto_soll,
            feiertag_minutes=feiertag_min,
            feiertag_tage=round(feiertag_tage, 2),
            sonstige_minutes=sonstige_min,
            sonstige_tage=round(sonstige_tage, 2),
        ),
    )


async def audit_log(
    db: AsyncSession, tenant_id: uuid.UUID, skip: int, limit: int
) -> list[AuditLogEntry]:
    """Return the full audit log for the tenant."""
    result = await db.execute(
        tenant_scoped(select(AuditLog), AuditLog, tenant_id)
        .order_by(AuditLog.created_at.desc())
        .offset(skip)
        .limit(limit)
    )
    logs = result.scalars().all()
    return [
        AuditLogEntry(
            id=log.id,
            actor_id=log.actor_id,
            action=log.action,
            resource_type=log.resource_type,
            resource_id=log.resource_id,
            ip_address=log.ip_address,
            details=log.details,
            created_at=log.created_at,
        )
        for log in logs
    ]


async def month_reconciliation(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    year: int,
    month: int,
    employee_id: uuid.UUID,
    *,
    has_view_all: bool,
    has_view_team: bool,
    caller_department_id: uuid.UUID | None,
    caller_department_ids: list[uuid.UUID] | None,
) -> MonthReconciliation:
    """Monatsabgleich EINES Mitarbeiters (Altsystem-Fussblock, Spec 2026-09-09).

    Reine Zusammenfuehrung vorhandener Rechnungen — nichts wird neu bewertet:
    Soll-Kette aus dem Stundenzettel (``monthly_timesheet``), Ueberstunden aus
    dem Ledger, Zuschlaege aus den Tagesboegen, Lohnarten-Zeilen aus derselben
    Funktion wie der LODAS-Export. Rechte wie ``monthly_timesheet`` (403 fuer
    Mitarbeiter ausserhalb des eigenen Bereichs).
    """
    from decimal import Decimal

    from zeiterfassung.models.export import ExportStatus, PayrollExport, WageTypeMapping
    from zeiterfassung.models.overtime import OvertimeEntry
    from zeiterfassung.models.user import User
    from zeiterfassung.payroll.categories import get_category
    from zeiterfassung.services import export_service, overtime_service

    sheet = await monthly_timesheet(
        db,
        tenant_id,
        year,
        month,
        employee_id,
        has_view_all=has_view_all,
        has_view_team=has_view_team,
        caller_department_id=caller_department_id,
        caller_department_ids=caller_department_ids,
    )
    user = await get_scoped(db, User, tenant_id, employee_id)
    s = sheet.summary
    ym = date(year, month, 1)
    month_end = (date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)) - timedelta(
        days=1
    )

    stunden = ReconciliationStunden(
        brutto_soll_minutes=s.brutto_soll_minutes,
        feiertag_minutes=s.feiertag_minutes,
        feiertag_tage=s.feiertag_tage,
        urlaub_minutes=s.urlaub_minutes,
        urlaub_tage=s.urlaub_tage,
        krank_minutes=s.krank_minutes,
        krank_tage=s.krank_tage,
        sonstige_minutes=s.sonstige_minutes,
        sonstige_tage=s.sonstige_tage,
        effektiv_soll_minutes=s.soll_minutes,
        ist_minutes=s.ist_minutes,
        saldo_minutes=s.saldo_minutes,
    )

    # ── Ueberstunden: Ledger vor dem Monat = Vortrag, Monatseintraege je Typ ──
    entries = (
        (
            await db.execute(
                tenant_scoped(select(OvertimeEntry), OvertimeEntry, tenant_id).where(
                    OvertimeEntry.employee_id == employee_id,
                )
            )
        )
        .scalars()
        .all()
    )
    # Vortrag = Live-Saldo zum Ende des Vormonats: Ledger-Eintraege vor dem
    # Monat plus Ist−Soll aller davor liegenden, noch nicht abgeschlossenen
    # Monate — dieselbe Rechnung wie der Konto-Saldo in der Uebersicht, nur
    # mit Stichtag Vormonatsende (sonst zeigte ein nie abgeschlossenes Konto
    # hier 0:00, waehrend die Zeile daneben den Live-Saldo fuehrt).
    vortrag = (
        await overtime_service.live_balance(
            db,
            tenant_id,
            user,
            today=ym - timedelta(days=1),
            entries=[e for e in entries if e.year_month < ym],
        )
        if user is not None
        else overtime_service._replay([e for e in entries if e.year_month < ym])
    )
    month_entries = [e for e in entries if e.year_month == ym]

    def _sum(typ: str) -> Decimal:
        return sum(
            (Decimal(str(e.hours)) for e in month_entries if e.entry_type == typ), Decimal(0)
        )

    abgeschlossen = any(e.entry_type == "accrual" for e in month_entries)
    monat = _sum("accrual") if abgeschlossen else Decimal(s.saldo_minutes) / Decimal(60)
    ausgezahlt = -_sum("payout")
    korrektur = _sum("adjustment")
    verfall = -_sum("forfeit")
    ueberstunden = ReconciliationUeberstunden(
        payout_mode=user.overtime_payout_mode if user else None,
        cap_hours=(
            float(user.overtime_cap_hours)
            if user is not None and user.overtime_cap_hours is not None
            else None
        ),
        vortrag_hours=float(vortrag),
        monat_hours=round(float(monat), 4),
        ausgezahlt_hours=float(ausgezahlt),
        korrektur_hours=float(korrektur),
        verfall_hours=float(verfall),
        saldo_hours=round(float(vortrag + monat - ausgezahlt + korrektur - verfall), 4),
        abgeschlossen=abgeschlossen,
    )

    # ── Zuschlaege aus den Tagesboegen (wie monthly_report) ──
    ts_rows = (
        (
            await db.execute(
                tenant_scoped(select(DailyTimesheet), DailyTimesheet, tenant_id).where(
                    DailyTimesheet.employee_id == employee_id,
                    DailyTimesheet.work_date >= ym,
                    DailyTimesheet.work_date <= month_end,
                )
            )
        )
        .scalars()
        .all()
    )
    zuschlaege = ReconciliationZuschlaege(
        nacht_minutes=sum(t.net_work_minutes for t in ts_rows if t.is_night_work),
        sonntag_minutes=sum(t.net_work_minutes for t in ts_rows if t.is_sunday_work),
        feiertag_minutes=sum(t.net_work_minutes for t in ts_rows if t.is_holiday_work),
        samstag_minutes=sum(t.net_work_minutes for t in ts_rows if t.work_date.weekday() == 5),
    )

    # ── Lohnarten-Zeilen: exakt die Export-Rechnung fuer diesen Mitarbeiter ──
    wage_types = await export_service._get_wage_type_mappings(db, tenant_id)
    datev_rows = await export_service._build_datev_rows(
        db, tenant_id, year, month, [employee_id], wage_types
    )
    labels: dict[int, str] = {}
    for code, nr in wage_types.items():
        if code.startswith("LA_"):
            continue
        try:
            labels.setdefault(nr, get_category(code).label)
        except (KeyError, ValueError):
            continue
    catalog = (
        (
            await db.execute(
                select(WageTypeMapping).where(
                    WageTypeMapping.tenant_id == tenant_id,
                    WageTypeMapping.datev_lohnart.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )
    for m in catalog:
        if m.description:
            labels[m.datev_lohnart] = m.description
    # Tage zu Abwesenheits-Lohnarten (Urlaub/Krank/Feiertag) aus der Soll-Kette;
    # Folgelohnarten (z. B. 150 zus. Urlaubsgeld) erben die Tage ihrer Basis.
    tage_by_code = {
        "URLAUB_STD": s.urlaub_tage,
        "FEIERTAG_BEZAHLT": s.feiertag_tage,
        "SONDERURLAUB_STD": s.sonstige_tage,
    }
    for code in (
        "KRANK_STD",
        "KRANK_EAU_TELEFON",
        "KRANK_MIT_AU",
        "KRANK_OHNE_AU_MIT_LFZ",
        "KRANK_OHNE_AU_OHNE_LFZ",
    ):
        tage_by_code[code] = s.krank_tage
    tage_by_lohnart: dict[int, float] = {}
    for code, days in tage_by_code.items():
        nr = wage_types.get(code)
        if nr is not None and days:
            tage_by_lohnart[nr] = days
    for m in catalog:
        if m.folge_lohnart and m.datev_lohnart in tage_by_lohnart:
            folge_nr = wage_types.get(m.folge_lohnart)
            if folge_nr is not None:
                tage_by_lohnart.setdefault(folge_nr, tage_by_lohnart[m.datev_lohnart])
    lohnarten = [
        ReconciliationLohnart(
            lohnart=r.lohnart,
            bezeichnung=labels.get(r.lohnart),
            wert=r.wert,
            tage=tage_by_lohnart.get(r.lohnart),
        )
        for r in datev_rows
    ]

    # Urlaubskonto: Resttage im Jahr des Monats (wie "01-Urlaubskonto" im Altsystem).
    try:
        urlaub_rest = float(
            (
                await absence_service.get_vacation_balance(db, tenant_id, employee_id, year)
            ).remaining_days
        )
    except Exception:  # noqa: BLE001 — Anzeige-Zusatz, darf den Abgleich nicht kippen
        urlaub_rest = None

    # ── Exportstatus des Monats (hoechster Stand) ──
    statuses = {
        st
        for st in (
            await db.execute(
                tenant_scoped(select(PayrollExport.status), PayrollExport, tenant_id).where(
                    PayrollExport.year_month == ym
                )
            )
        )
        .scalars()
        .all()
    }
    export_status: str | None = None
    if ExportStatus.DELIVERED in statuses:
        export_status = ExportStatus.DELIVERED.value
    elif ExportStatus.GENERATED in statuses:
        export_status = ExportStatus.GENERATED.value

    return MonthReconciliation(
        employee_id=sheet.employee_id,
        employee_name=sheet.employee_name,
        employee_number=sheet.employee_number,
        year=year,
        month=month,
        stunden=stunden,
        ueberstunden=ueberstunden,
        zuschlaege=zuschlaege,
        lohnarten=lohnarten,
        export_status=export_status,
        urlaub_rest_tage=urlaub_rest,
    )
