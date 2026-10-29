"""Absence and leave request service."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from zeiterfassung.core.errors import DomainError, ErrorCode
from zeiterfassung.core.holidays import working_days_between
from zeiterfassung.db.scoping import get_scoped
from zeiterfassung.models.absence import (
    ActorType,
    ApprovalAuditLog,
    ApprovalRequest,
    RequestStatus,
    RequestType,
    SickStatus,
)
from zeiterfassung.policy import check as policy_check
from zeiterfassung.policy import permissions as policy_permissions
from zeiterfassung.policy.context import AbsenceEntry
from zeiterfassung.policy.overtime import weekly_target_minutes
from zeiterfassung.policy.tagesregeln import Tagesregeln
from zeiterfassung.schemas.absence import AbsenceRequestCreate, VacationBalance
from zeiterfassung.services import audit_service

if TYPE_CHECKING:
    from zeiterfassung.api.deps import ScopeContext


def strip_medical_fields(response: dict, has_medical_read: bool) -> dict:
    """Remove sick leave details if the caller lacks ``medical:read`` permission.

    Protects sensitive data (notes, payload) from roles that can see absence
    records but are not authorised to view medical information (e.g. Teamleiter,
    Buchhaltung).  HR (``medical:read``) sees the full record.

    Args:
        response: A mutable dict representation of an ``AbsenceRequestResponse``.
        has_medical_read: Whether the calling user holds the ``medical:read``
            permission.

    Returns:
        The (mutated) dict with sensitive fields removed when appropriate.
    """
    if not has_medical_read and response.get("request_type") == "SICK_LEAVE":
        response["notes"] = None
        response.pop("payload", None)
    return response


class RequestNotFoundError(DomainError):
    code = ErrorCode.ABSENCE_REQUEST_NOT_FOUND


class InvalidStatusTransitionError(DomainError):
    code = ErrorCode.ABSENCE_INVALID_STATUS_TRANSITION


class ConflictError(DomainError):
    code = ErrorCode.ABSENCE_CONFLICT


class SelfApprovalError(DomainError):
    code = ErrorCode.ABSENCE_SELF_APPROVAL


# Last-resort default annual vacation entitlement (days) — used only when a
# tenant hasn't configured its own default (see resolve_default_vacation_entitlement_days).
DEFAULT_VACATION_DAYS_PER_YEAR = 24.0


def resolve_default_vacation_entitlement_days(tenant: object | None) -> float:
    """Per-tenant configurable default (``tenant.settings["default_vacation_entitlement_days"]``).

    Falls back to :data:`DEFAULT_VACATION_DAYS_PER_YEAR` when the tenant has none
    configured (or ``tenant`` is ``None``). Single source of truth for both
    new-employee creation (``user_service.create_user``) and the balance
    fallback below (7tu: the two used to disagree — 30 vs. 24).
    """
    if tenant is not None:
        configured = (tenant.settings or {}).get("default_vacation_entitlement_days")
        if configured is not None:
            return float(configured)
    return DEFAULT_VACATION_DAYS_PER_YEAR


async def _auto_resettle_for_absence(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    employee_id: uuid.UUID,
    affected_date: date,
    actor_id: uuid.UUID | None,
) -> None:
    """Betroffenen, bereits abgeschlossenen Monat nach einer Abwesenheits-Änderung neu abschließen.

    Nur GENEHMIGTE Abwesenheiten wirken sich auf den Überstunden-Saldo aus (siehe
    ``_daily_rows_for_month`` / policy.classify_day) — ein Übergang, der eine
    genehmigte Abwesenheit schafft oder entfernt, kann also einen bereits
    abgeschlossenen Monat verändern. Fehler dürfen den Absence-Schreibpfad nie
    brechen: ``overtime_service.auto_resettle_if_needed`` fängt selbst alles ab.
    """
    from zeiterfassung.services import overtime_service

    await overtime_service.auto_resettle_if_needed(
        db, tenant_id, employee_id, affected_date, actor_id
    )


async def create_request(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    employee_id: uuid.UUID,
    data: AbsenceRequestCreate,
    bundesland: str | None = None,
    auto_approve: bool = False,
    approver_id: uuid.UUID | None = None,
) -> ApprovalRequest:
    """Create a new leave request.

    Normally created in SUBMITTED status (awaits approval). With
    ``auto_approve=True`` (e.g. a manager who holds approval authority records
    their own absence) it is created already APPROVED — no separate 4-eyes step.
    """
    # Teil-Tag in Stunden nur für EINEN Tag. Steht auch im Schema-Validator,
    # muss aber hier wiederholt werden: der Admin-Pfad baut das Schema per
    # model_construct (Vergangenheits-Guard-Bypass) und überspringt damit ALLE
    # Validatoren.
    if data.hours is not None:
        if data.date_from != data.date_to:
            raise ConflictError("Teil-Tag in Stunden gilt nur für einen einzelnen Tag (Von = Bis)")
        if not 0 < data.hours <= 24:
            raise ConflictError("Stunden müssen zwischen 0 und 24 liegen")
    # Sonderurlaub verlangt IMMER eine Begründung (Spec 2026-08-11). Hier statt
    # im Schema, damit auch der Admin-Pfad (model_construct) sie erzwingt.
    if data.request_type == RequestType.SPECIAL_LEAVE and not (data.notes or "").strip():
        raise ConflictError("Sonderurlaub braucht eine Begründung (z. B. Hochzeit, Beerdigung)")
    # ── R-A3: Überschneidungsprüfung (halbtags-genau) über die Policy-Schicht.
    # Wir laden die noch "lebenden" Abwesenheiten im überlappenden Zeitraum und
    # lassen die Regel entscheiden (Urlaub kollidiert auch mit Krank etc.).
    existing = await _living_absences_overlapping(
        db, tenant_id, employee_id, data.date_from, data.date_to
    )
    new_entry = AbsenceEntry(
        date_from=data.date_from,
        date_to=data.date_to,
        day_part=data.day_part,
        request_type=str(data.request_type),
        status=RequestStatus.SUBMITTED.value,
        request_id=None,
    )
    for v in policy_check.check_new_absence(new_entry, existing):
        if v.is_block:
            # Meldung enthält den belegten Zeitraum → Frontend zeigt "erst löschen".
            raise ConflictError(v.message)

    # Tages-Anteil: bei explizit angegebenen Stunden (Halbtag-Urlaub) aus den
    # Stunden ableiten (Stunden / Soll pro Tag), sonst über den day_part-Anteil
    # (1,0 ganzer Tag / 0,5 Halbtag). Feiertage/Wochenende via working_days_between.
    if data.hours is not None:
        from zeiterfassung.models.user import User
        from zeiterfassung.models.work_time_profile import WorkTimeProfile

        emp = (await db.execute(select(User).where(User.id == employee_id))).scalar_one_or_none()
        wt_profile = (
            await get_scoped(db, WorkTimeProfile, tenant_id, emp.work_time_profile_id)
            if emp is not None and emp.work_time_profile_id
            else None
        )
        # Stunden-Teil-Tag gilt nur für EINEN Tag → Soll dieses Wochentags,
        # aus der EINEN Stelle (policy.tagesregeln, inkl. Arbeitszeit-Profil).
        daily = (Tagesregeln.fuer(emp, wt_profile).tagessoll(data.date_from) if emp else 0) / 60.0
        frac = max(0.0, min(1.0, data.hours / daily if daily else 0.5))
    else:
        frac = data.day_part.fraction
    if bundesland is None:
        from zeiterfassung.services.tenant_service import resolve_bundesland

        bundesland = await resolve_bundesland(db, tenant_id)
    days = working_days_between(data.date_from, data.date_to, bundesland) * frac
    now = datetime.now(tz=UTC)
    new_status = RequestStatus.APPROVED if auto_approve else RequestStatus.SUBMITTED
    request = ApprovalRequest(
        tenant_id=tenant_id,
        employee_id=employee_id,
        request_type=data.request_type,
        status=new_status,
        date_from=data.date_from,
        date_to=data.date_to,
        day_part=data.day_part,
        days_count=Decimal(str(days)),
        # AU/LFZ-Status nur bei Krankmeldung übernehmen.
        sick_status=(data.sick_status if data.request_type == RequestType.SICK_LEAVE else None),
        notes=data.notes,
        payload=data.payload,
        submitted_at=now,
        # Genehmiger = handelnder Akteur (Admin/HR); Fallback: der MA selbst (GF-Selbsteintrag).
        decided_by=(approver_id or employee_id) if auto_approve else None,
        decided_at=now if auto_approve else None,
        created_at=now,
        updated_at=now,
    )
    db.add(request)
    await db.flush()

    # Regel: an (genehmigten) Krankheitstagen dürfen keine automatischen/gesetzten
    # Arbeitszeiten stehen. Echte Stempel-Tage (computed_from_events) bleiben unberührt.
    if data.request_type == RequestType.SICK_LEAVE and new_status == RequestStatus.APPROVED:
        from sqlalchemy import delete as _delete

        from zeiterfassung.models.clock import DailyTimesheet

        await db.execute(
            _delete(DailyTimesheet).where(
                DailyTimesheet.tenant_id == tenant_id,
                DailyTimesheet.employee_id == employee_id,
                DailyTimesheet.work_date >= data.date_from,
                DailyTimesheet.work_date <= data.date_to,
                DailyTimesheet.computed_from_events.is_(False),
            )
        )

    await _append_audit_log(
        db,
        request_id=request.id,
        old_status=None,
        new_status=new_status,
        actor_id=employee_id,
        actor_type=ActorType.USER,
    )
    if new_status == RequestStatus.APPROVED:
        await _auto_resettle_for_absence(
            db, tenant_id, employee_id, data.date_from, approver_id or employee_id
        )
    return request


async def get_request(
    db: AsyncSession, tenant_id: uuid.UUID, request_id: uuid.UUID
) -> ApprovalRequest:
    result = await db.execute(
        select(ApprovalRequest).where(
            ApprovalRequest.id == request_id,
            ApprovalRequest.tenant_id == tenant_id,
        )
    )
    req = result.scalar_one_or_none()
    if req is None:
        raise RequestNotFoundError(f"Request {request_id} not found")
    return req


async def list_requests(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    employee_id: uuid.UUID | None = None,
    status: RequestStatus | None = None,
    skip: int = 0,
    limit: int = 100,
    scope: ScopeContext | None = None,
) -> list[ApprovalRequest]:
    """List absence requests with optional scope-based data filtering.

    When *scope* is provided it takes precedence over *employee_id* for
    determining which employees' requests are returned.
    """
    from zeiterfassung.models.user import User

    # Anonymized scope: Betriebsrat sees no individual requests
    if scope is not None and scope.is_anonymized:
        return []

    stmt = select(ApprovalRequest).where(ApprovalRequest.tenant_id == tenant_id)

    if scope is not None:
        if scope.is_self_only:
            stmt = stmt.where(ApprovalRequest.employee_id == scope.user_id)
        elif scope.is_department:
            if not scope.dept_ids:
                stmt = stmt.where(ApprovalRequest.employee_id == scope.user_id)
            else:
                stmt = stmt.join(User, ApprovalRequest.employee_id == User.id).where(
                    User.department_id.in_(scope.dept_ids)
                )
        # is_global: no employee filter

    # employee_id engt ZUSÄTZLICH zum Scope ein (AND) — kann Rechte nur
    # verkleinern, nie erweitern (Self-Scope + fremde ID ⇒ leere Liste).
    # Ohne Scope ist es der Legacy-Pfad der Admin-Endpunkte.
    if employee_id is not None:
        stmt = stmt.where(ApprovalRequest.employee_id == employee_id)

    if status is not None:
        stmt = stmt.where(ApprovalRequest.status == status)
    stmt = stmt.order_by(ApprovalRequest.created_at.desc()).offset(skip).limit(limit)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def approve_request(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    request_id: uuid.UUID,
    approver_id: uuid.UUID,
    reason: str | None = None,
) -> ApprovalRequest:
    req = await get_request(db, tenant_id, request_id)
    # ── R-A4: Vier-Augen-Prinzip — Entscheidung liegt in der Policy-Schicht ──
    if policy_permissions.check_not_self_approval(req.employee_id, approver_id) is not None:
        raise SelfApprovalError("Cannot approve your own absence request")
    if req.status not in (RequestStatus.SUBMITTED, RequestStatus.ESCALATED):
        raise InvalidStatusTransitionError(f"Cannot approve a request in status {req.status}")
    old_status = req.status
    now = datetime.now(tz=UTC)
    req.status = RequestStatus.APPROVED
    req.decided_by = approver_id
    req.decided_at = now
    req.decision_reason = reason
    req.updated_at = now
    await db.flush()

    await _append_audit_log(
        db,
        request_id=req.id,
        old_status=old_status,
        new_status=RequestStatus.APPROVED,
        actor_id=approver_id,
        actor_type=ActorType.USER,
        notes=reason,
    )
    await audit_service.log(
        db,
        tenant_id=tenant_id,
        actor_id=approver_id,
        action="leave.approve",
        resource_type="absence_request",
        resource_id=request_id,
        details={"employee_id": str(req.employee_id)},
    )
    await _auto_resettle_for_absence(db, tenant_id, req.employee_id, req.date_from, approver_id)
    return req


async def reject_request(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    request_id: uuid.UUID,
    approver_id: uuid.UUID,
    reason: str,
) -> ApprovalRequest:
    req = await get_request(db, tenant_id, request_id)
    if req.status not in (RequestStatus.SUBMITTED, RequestStatus.ESCALATED):
        raise InvalidStatusTransitionError(f"Cannot reject a request in status {req.status}")
    old_status = req.status
    now = datetime.now(tz=UTC)
    req.status = RequestStatus.REJECTED
    req.decided_by = approver_id
    req.decided_at = now
    req.decision_reason = reason
    req.updated_at = now
    await db.flush()

    await _append_audit_log(
        db,
        request_id=req.id,
        old_status=old_status,
        new_status=RequestStatus.REJECTED,
        actor_id=approver_id,
        actor_type=ActorType.USER,
        notes=reason,
    )
    await audit_service.log(
        db,
        tenant_id=tenant_id,
        actor_id=approver_id,
        action="leave.reject",
        resource_type="absence_request",
        resource_id=request_id,
        details={"employee_id": str(req.employee_id)},
    )
    return req


async def cancel_request(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    request_id: uuid.UUID,
    employee_id: uuid.UUID,
) -> ApprovalRequest:
    req = await get_request(db, tenant_id, request_id)
    if req.employee_id != employee_id:
        raise RequestNotFoundError("Request not found")
    if req.status == RequestStatus.APPROVED:
        raise InvalidStatusTransitionError(f"Cannot cancel a request in status {req.status}")
    old_status = req.status
    req.status = RequestStatus.CANCELLED
    req.updated_at = datetime.now(tz=UTC)
    await db.flush()

    await _append_audit_log(
        db,
        request_id=req.id,
        old_status=old_status,
        new_status=RequestStatus.CANCELLED,
        actor_id=employee_id,
        actor_type=ActorType.USER,
    )
    return req


async def update_request_period(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    request_id: uuid.UUID,
    date_from: date,
    date_to: date,
    actor_id: uuid.UUID | None,
) -> ApprovalRequest:
    """Zeitraum einer Abwesenheit aendern (z.B. frueher zurueck aus Krank).

    Erlaubt fuer eingereichte UND genehmigte Antraege; Tage werden neu
    berechnet, Ueberschneidungen geprueft, die Aenderung im Audit-Log
    dokumentiert (alter -> neuer Zeitraum).
    """
    req = await get_request(db, tenant_id, request_id)
    if req.status not in (
        RequestStatus.SUBMITTED,
        RequestStatus.APPROVED,
        RequestStatus.ESCALATED,
    ):
        raise InvalidStatusTransitionError(f"Zeitraum nicht aenderbar in Status {req.status}")
    if date_to < date_from:
        raise ConflictError("Bis-Datum liegt vor dem Von-Datum")

    # Ueberschneidung mit ANDEREN lebenden Abwesenheiten pruefen.
    existing = [
        e
        for e in await _living_absences_overlapping(
            db, tenant_id, req.employee_id, date_from, date_to
        )
        if e.request_id != str(req.id)
    ]
    new_entry = AbsenceEntry(
        date_from=date_from,
        date_to=date_to,
        day_part=req.day_part,
        request_type=str(req.request_type),
        status=str(req.status),
        request_id=str(req.id),
    )
    for v in policy_check.check_new_absence(new_entry, existing):
        if v.is_block:
            raise ConflictError(v.message)

    old_range = f"{req.date_from.isoformat()} – {req.date_to.isoformat()}"
    from zeiterfassung.services.tenant_service import resolve_bundesland

    bundesland = await resolve_bundesland(db, tenant_id)
    # Effektiven Tages-Anteil aus dem Bestand ableiten statt nur aus day_part:
    # ein stundenbasierter Teil-Tag (z. B. 4 h → days_count 0,5 bei day_part=FULL)
    # trägt seinen Anteil nur in days_count. Sonst würde die Neuberechnung ihn
    # still auf einen vollen Tag aufrunden.
    frac = req.day_part.fraction if req.day_part else 1.0
    old_wd = working_days_between(req.date_from, req.date_to, bundesland)
    if old_wd:
        eff = float(req.days_count) / old_wd
        if abs(eff - frac) > 1e-9:
            # Stundenbasierter Teil-Tag: gilt nur für einen einzelnen Tag.
            if date_from != date_to:
                raise ConflictError(
                    "Teil-Tag in Stunden kann nicht auf mehrere Tage geändert werden"
                )
            frac = eff
    req.date_from = date_from
    req.date_to = date_to
    req.days_count = Decimal(str(working_days_between(date_from, date_to, bundesland) * frac))
    req.updated_at = datetime.now(tz=UTC)
    await db.flush()

    await _append_audit_log(
        db,
        request_id=req.id,
        old_status=req.status,
        new_status=req.status,
        actor_id=actor_id or req.employee_id,
        actor_type=ActorType.USER,
        notes=f"Zeitraum geaendert: {old_range} -> {date_from.isoformat()} – {date_to.isoformat()}",
    )
    return req


async def set_sick_status(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    request_id: uuid.UUID,
    sick_status: SickStatus | None,
    actor_id: uuid.UUID | None,
) -> ApprovalRequest:
    """Art der Krankmeldung (AU/LFZ) an einer bestehenden Krankmeldung setzen/ändern.

    Nur für SICK_LEAVE erlaubt; die Änderung wird im Audit-Log festgehalten.
    """
    req = await get_request(db, tenant_id, request_id)
    if req.request_type != RequestType.SICK_LEAVE:
        raise ConflictError("AU-Status gibt es nur für Krankmeldungen")
    old = req.sick_status
    req.sick_status = sick_status
    req.updated_at = datetime.now(tz=UTC)
    await db.flush()
    await _append_audit_log(
        db,
        request_id=req.id,
        old_status=req.status,
        new_status=req.status,
        actor_id=actor_id or req.employee_id,
        actor_type=ActorType.USER,
        notes=f"AU-Status geaendert: {old} -> {sick_status}",
    )
    return req


async def revoke_approval(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    request_id: uuid.UUID,
    approver_id: uuid.UUID,
    reason: str | None = None,
) -> ApprovalRequest:
    """Revoke an approved OR still-submitted absence request (TL/HR action).

    Used when an employee clocks in during approved vacation/sick leave, when
    circumstances change — or to clear a kiosk-submitted request of an
    employee who has no own login. Creates an audit trail entry.
    """
    req = await get_request(db, tenant_id, request_id)
    if req.status not in (RequestStatus.APPROVED, RequestStatus.SUBMITTED):
        raise InvalidStatusTransitionError(
            f"Only approved or submitted requests can be revoked (current: {req.status})"
        )
    old_status = req.status
    req.status = RequestStatus.CANCELLED
    req.decision_reason = reason or "Genehmigung widerrufen"
    req.updated_at = datetime.now(tz=UTC)
    await db.flush()

    await _append_audit_log(
        db,
        request_id=req.id,
        old_status=old_status,
        new_status=RequestStatus.CANCELLED,
        actor_id=approver_id,
        actor_type=ActorType.USER,
        notes=reason or "Genehmigung widerrufen durch Vorgesetzten",
    )
    if old_status == RequestStatus.APPROVED:
        # Eine genehmigte Abwesenheit wurde entfernt -> betrifft ggf. den Saldo.
        await _auto_resettle_for_absence(db, tenant_id, req.employee_id, req.date_from, approver_id)
    return req


async def request_cancellation(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    request_id: uuid.UUID,
    employee_id: uuid.UUID,
    reason: str | None = None,
) -> ApprovalRequest:
    """Employee asks to cancel their own APPROVED, not-yet-past request.

    Sets a flag in the request payload; an approver then revokes or declines it.
    Direct cancellation of approved leave by the employee is intentionally not
    allowed (you cannot just cancel anything).
    """
    req = await get_request(db, tenant_id, request_id)
    if req.employee_id != employee_id:
        raise RequestNotFoundError("Request not found")
    if req.status != RequestStatus.APPROVED:
        raise InvalidStatusTransitionError(
            "Nur genehmigte Antraege koennen zur Stornierung beantragt werden "
            f"(aktuell: {req.status})"
        )
    if req.date_to < date.today():
        raise InvalidStatusTransitionError("Vergangene Antraege koennen nicht storniert werden")
    payload = dict(req.payload or {})
    payload["cancel_requested"] = True
    if reason:
        payload["cancel_reason"] = reason
    req.payload = payload
    req.updated_at = datetime.now(tz=UTC)
    await db.flush()
    await audit_service.log(
        db,
        tenant_id=tenant_id,
        actor_id=employee_id,
        action="leave.cancel_requested",
        resource_type="absence_request",
        resource_id=request_id,
        details={"reason": reason} if reason else None,
    )
    return req


async def decline_cancellation(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    request_id: uuid.UUID,
    approver_id: uuid.UUID,
) -> ApprovalRequest:
    """Approver declines an employee's cancellation request; stays APPROVED."""
    req = await get_request(db, tenant_id, request_id)
    payload = dict(req.payload or {})
    payload["cancel_requested"] = False
    req.payload = payload
    req.updated_at = datetime.now(tz=UTC)
    await db.flush()
    await audit_service.log(
        db,
        tenant_id=tenant_id,
        actor_id=approver_id,
        action="leave.cancel_declined",
        resource_type="absence_request",
        resource_id=request_id,
    )
    return req


async def list_cancellation_requests(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    scope: ScopeContext,
) -> list[ApprovalRequest]:
    """APPROVED requests with a pending employee cancellation flag, scope-filtered."""
    from zeiterfassung.models.user import User

    stmt = select(ApprovalRequest).where(
        ApprovalRequest.tenant_id == tenant_id,
        ApprovalRequest.status == RequestStatus.APPROVED,
    )
    if scope.is_department:
        if not scope.dept_ids:
            return []
        stmt = stmt.join(User, ApprovalRequest.employee_id == User.id).where(
            User.department_id.in_(scope.dept_ids)
        )
    elif not scope.is_global:
        return []
    result = await db.execute(stmt)
    return [r for r in result.scalars().all() if (r.payload or {}).get("cancel_requested")]


async def get_vacation_balance_for_scope(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    target_employee_id: uuid.UUID,
    year: int,
    scope: ScopeContext,
) -> VacationBalance:
    """Return vacation balance for *target_employee_id* after checking scope.

    Raises ``PermissionError`` when the caller's scope does not permit viewing
    another employee's balance.
    """
    from zeiterfassung.models.user import User

    # Self-only: can only view own balance
    if scope.is_self_only and target_employee_id != scope.user_id:
        raise PermissionError("Insufficient scope to view another employee's vacation balance")

    # Anonymized: no individual balance
    if scope.is_anonymized:
        raise PermissionError("Anonymized scope cannot access individual vacation balances")

    # Department scope: target must be in same department
    if scope.is_department and target_employee_id != scope.user_id:
        if not scope.dept_ids:
            raise PermissionError("Department scope but no department_id in token")
        result = await db.execute(
            select(User).where(
                User.id == target_employee_id,
                User.tenant_id == tenant_id,
                User.department_id.in_(scope.dept_ids),
            )
        )
        if result.scalar_one_or_none() is None:
            raise PermissionError("Target employee is not in your department")

    return await get_vacation_balance(db, tenant_id, target_employee_id, year)


async def _vacation_worked_refund(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    employee_id: uuid.UUID,
    approved_vacation: list[ApprovalRequest],
) -> tuple[float, float]:
    """Urlaubstage, an denen tatsächlich gearbeitet wurde, zur Rückgabe (#22).

    Ein VOLLER (Anteil ≥ 1,0) genehmigter Urlaubstag, an dem ein Tagesbogen mit
    echter Arbeitszeit existiert, wird zurückgegeben — die Stunden zählen dann
    normal im Arbeitszeitkonto (siehe :func:`policy.overtime.classify_day`).
    Halbe Urlaubstage sind der geplante Normalfall (halb frei, halb gearbeitet)
    und werden NICHT zurückgegeben.

    Rückgabe: ``(refund_total, refund_past)`` in Tagen — ``refund_past`` ist der
    Anteil auf Tagen vor heute (für die genommen/geplant-Aufteilung).
    """
    from zeiterfassung.models.clock import DailyTimesheet

    if not approved_vacation:
        return 0.0, 0.0

    span_start = min(r.date_from for r in approved_vacation)
    span_end = max(r.date_to for r in approved_vacation)
    ts_rows = (
        await db.execute(
            select(
                DailyTimesheet.work_date, DailyTimesheet.net_work_minutes, DailyTimesheet.start_time
            ).where(
                DailyTimesheet.tenant_id == tenant_id,
                DailyTimesheet.employee_id == employee_id,
                DailyTimesheet.work_date >= span_start,
                DailyTimesheet.work_date <= span_end,
            )
        )
    ).all()
    # Nur Tage mit echter Arbeit (Netto > 0 oder ein Einstempel-Zeitpunkt).
    worked_dates = {
        r.work_date for r in ts_rows if (r.net_work_minutes or 0) > 0 or r.start_time is not None
    }
    if not worked_dates:
        return 0.0, 0.0

    today = date.today()
    refund_total = 0.0
    refund_past = 0.0
    for r in approved_vacation:
        day_frac = Tagesregeln.abwesenheits_deckung(r)
        if day_frac < 0.999:
            continue  # halber Urlaubstag = Normalfall, keine Rückgabe
        d = r.date_from
        while d <= r.date_to:
            if d in worked_dates:
                refund_total += 1.0
                if d < today:
                    refund_past += 1.0
            d += timedelta(days=1)
    return refund_total, refund_past


async def get_vacation_balance(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    employee_id: uuid.UUID,
    year: int,
    *,
    today: date | None = None,
) -> VacationBalance:
    """Return vacation balance for an employee in a given year.

    ``today`` ist injizierbar (Tests); ohne Angabe gilt das heutige Datum.
    """
    year_start = date(year, 1, 1)
    year_end = date(year, 12, 31)

    result = await db.execute(
        select(ApprovalRequest).where(
            ApprovalRequest.tenant_id == tenant_id,
            ApprovalRequest.employee_id == employee_id,
            ApprovalRequest.request_type == RequestType.VACATION,
            ApprovalRequest.date_from >= year_start,
            ApprovalRequest.date_from <= year_end,
            ApprovalRequest.status.in_(
                [RequestStatus.APPROVED, RequestStatus.SUBMITTED, RequestStatus.ESCALATED]
            ),
        )
    )
    requests = result.scalars().all()

    used_days = float(sum(r.days_count for r in requests if r.status == RequestStatus.APPROVED))
    # #22: Wurde an einem GENEHMIGTEN (vollen) Urlaubstag tatsächlich gearbeitet,
    # wird der Urlaub für den Tag zurückgegeben — die Stunden zählen normal (siehe
    # policy.overtime.classify_day). Wir mutieren den Antrag nicht, sondern ziehen
    # den gearbeiteten Anteil hier vom Verbrauch ab.
    refund_days, refund_past = await _vacation_worked_refund(
        db,
        tenant_id,
        employee_id,
        [r for r in requests if r.status == RequestStatus.APPROVED],
    )
    used_days = max(0.0, used_days - refund_days)
    pending_days = float(
        sum(
            r.days_count
            for r in requests
            if r.status in (RequestStatus.SUBMITTED, RequestStatus.ESCALATED)
        )
    )
    # Aufteilung des Verbrauchs: bereits GENOMMEN (genehmigter Urlaub in der
    # Vergangenheit) vs. GEPLANT (genehmigt/beantragt, heute oder künftig).
    # offen + genommen + geplant = Anspruch.
    if today is None:
        today = date.today()
    # Ein Urlaubsblock, der ueber heute hinauslaeuft, ist TEILS genommen: die
    # Tage bis gestern sind gelebt, ab heute stehen sie noch bevor. Frueher galt
    # der Block nur komplett als genommen (date_to < today) — bei 12.08.-18.09.
    # standen dadurch auch die laengst vergangenen August-Tage als "geplant".
    # Der heutige Tag zaehlt bewusst NOCH als geplant.
    from zeiterfassung.services.tenant_service import resolve_bundesland

    gestern = today - timedelta(days=1)
    bundesland = await resolve_bundesland(db, tenant_id)
    past_days = 0.0
    for r in requests:
        if r.status != RequestStatus.APPROVED or r.date_from > gestern:
            continue
        if r.date_to <= gestern:
            past_days += float(r.days_count)  # ganz vorbei
            continue
        # Teils vorbei: anteilig ueber die ARBEITSTAGE, damit Wochenenden,
        # Feiertage und Halbtage (days_count < Kalendertage) stimmen.
        gesamt = working_days_between(r.date_from, r.date_to, bundesland)
        vergangen = working_days_between(r.date_from, gestern, bundesland)
        if gesamt > 0:
            past_days += float(r.days_count) * (vergangen / gesamt)
    # Gearbeitete Urlaubstage sind vergangene Tage → auch aus „genommen" abziehen,
    # sonst würde „geplant" (= genommen aus Zukunft) rechnerisch negativ.
    past_days = max(0.0, past_days - refund_past)
    planned_days = (used_days - past_days) + pending_days
    # Anspruch aus dem Mitarbeiter-Feld (vacation_entitlement_days), nicht fix.
    from zeiterfassung.models.tenant import Tenant
    from zeiterfassung.models.user import User

    emp = (await db.execute(select(User).where(User.id == employee_id))).scalar_one_or_none()
    if emp is not None and emp.vacation_entitlement_days is not None:
        entitlement = float(emp.vacation_entitlement_days)
    else:
        result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
        entitlement = resolve_default_vacation_entitlement_days(result.scalar_one_or_none())
    # Startjahr: der beim Systemstart erfasste Resturlaub ist die Bilanz-Basis
    # (verbleibend), nicht der volle Jahresanspruch — ab dem Folgejahr gilt
    # wieder der volle Anspruch.
    #
    # Startjahr = Jahr des ERFASSUNGSBEGINNS (counting_start_date). Beginnt die
    # Erfassung mitten im Jahr (Go-Live 07/2026), ist der uebernommene Reststand
    # aus der alten Lohnabrechnung die Basis — nicht der Jahresanspruch und auch
    # nicht das Eintrittsjahr (das kann Jahrzehnte zurueckliegen). Ohne
    # counting_start_date bleibt das Eintrittsjahr die Referenz (Alt-Verhalten
    # fuer Mitarbeiter, die waehrend des Betriebs neu dazukommen).
    base_year = None
    if emp is not None:
        if emp.counting_start_date is not None:
            base_year = emp.counting_start_date.year
        elif emp.hire_date is not None:
            base_year = emp.hire_date.year
    if emp is not None and emp.initial_vacation_remaining_days is not None and base_year == year:
        entitlement = float(emp.initial_vacation_remaining_days)
    # Resturlaub = Anspruch minus GENOMMENE Tage. Genehmigte kuenftige Tage
    # (planned) und Antraege (pending) sind Teil des Rests — "davon geplant" —
    # und druecken die Zahl nicht: 35, 18 genommen, 1 geplant -> Rest 17.
    remaining = entitlement - past_days

    # Stunden-Sicht: durchschnittliches Tages-Soll (Wochen-Soll / 5). Bei
    # wochentagsspezifischem Plan ist das der Schnitt; days_count trägt die Tage.
    weekly_min = weekly_target_minutes(
        emp.contracted_hours_per_week if emp else None,
        emp.weekday_target_minutes if emp else None,
    )
    daily_hours = weekly_min / 5.0 / 60.0

    return VacationBalance(
        employee_id=employee_id,
        year=year,
        entitlement_days=entitlement,
        used_days=used_days,
        pending_days=pending_days,
        past_days=past_days,
        planned_days=planned_days,
        remaining_days=max(0.0, remaining),
        entitlement_hours=entitlement * daily_hours,
        used_hours=used_days * daily_hours,
        pending_hours=pending_days * daily_hours,
        remaining_hours=max(0.0, remaining) * daily_hours,
    )


async def remaining_days_for_scope(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    year: int,
    scope: ScopeContext,
) -> dict[uuid.UUID, float]:
    """Resturlaub (Tage) je aktivem Mitarbeiter im Scope — für Listen.

    Bewusst ein Aufruf von :func:`get_vacation_balance` je Mitarbeiter: die
    Bilanz-Regeln (Eintrittsjahr, gearbeitete Urlaubstage, beantragt vs.
    genehmigt) bleiben so an EINER Stelle. Selbst-/anonymisierter Scope sieht
    keine fremden Salden (wie ``/clock/presence``, ADR-007).
    """
    from zeiterfassung.models.user import User

    if scope.is_self_only or scope.is_anonymized:
        return {}

    stmt = select(User.id).where(
        User.tenant_id == tenant_id,
        User.is_active == True,  # noqa: E712
    )
    if scope.is_department:
        if not scope.dept_ids:
            return {}
        stmt = stmt.where(User.department_id.in_(scope.dept_ids))

    employee_ids = list((await db.execute(stmt)).scalars().all())
    out: dict[uuid.UUID, float] = {}
    for employee_id in employee_ids:
        balance = await get_vacation_balance(db, tenant_id, employee_id, year)
        out[employee_id] = balance.remaining_days
    return out


async def get_calendar(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    date_from: date,
    date_to: date,
    scope: ScopeContext | None = None,
    employee_id: uuid.UUID | None = None,
) -> list[ApprovalRequest]:
    """Return absence requests overlapping the given date range, filtered by scope.

    Anonymized scope (Betriebsrat) returns an empty list — they use the
    aggregate BR endpoints instead.
    """
    from zeiterfassung.models.user import User

    if scope is not None and scope.is_anonymized:
        return []

    stmt = select(ApprovalRequest).where(
        ApprovalRequest.tenant_id == tenant_id,
        ApprovalRequest.date_from <= date_to,
        ApprovalRequest.date_to >= date_from,
        ApprovalRequest.status.in_([RequestStatus.APPROVED, RequestStatus.SUBMITTED]),
    )
    # Optional auf EINEN Mitarbeiter einschränken (z. B. Mitarbeiterprofil).
    if employee_id is not None:
        stmt = stmt.where(ApprovalRequest.employee_id == employee_id)

    if scope is not None:
        if scope.is_self_only:
            stmt = stmt.where(ApprovalRequest.employee_id == scope.user_id)
        elif scope.is_department:
            if not scope.dept_ids:
                stmt = stmt.where(ApprovalRequest.employee_id == scope.user_id)
            else:
                stmt = stmt.join(User, ApprovalRequest.employee_id == User.id).where(
                    User.department_id.in_(scope.dept_ids)
                )
        # is_global: return all (no extra filter)

    stmt = stmt.order_by(ApprovalRequest.date_from)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def team_capacity_check(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    department_id: uuid.UUID,
    date_from: date,
    date_to: date,
    exclude_employee_id: uuid.UUID | None = None,
) -> dict:
    """Check how many team members are already absent during a date range.

    Returns:
        {
            "total_employees": int,      # active employees in department
            "absent_count": int,          # number with approved/submitted absence overlapping
            "absent_names": list[str],    # first names of absent employees (for info)
            "capacity_pct": float,        # percentage of team absent
            "date_from": str,
            "date_to": str,
        }
    """
    from zeiterfassung.models.user import User

    # Count active employees in the department
    total_result = await db.execute(
        select(func.count())
        .select_from(User)
        .where(
            User.tenant_id == tenant_id,
            User.department_id == department_id,
            User.is_active.is_(True),
        )
    )
    total_employees: int = total_result.scalar_one()

    # Find approved/submitted absences overlapping the date range for that department
    stmt = (
        select(ApprovalRequest, User.first_name)
        .join(User, ApprovalRequest.employee_id == User.id)
        .where(
            ApprovalRequest.tenant_id == tenant_id,
            User.department_id == department_id,
            User.is_active.is_(True),
            ApprovalRequest.date_from <= date_to,
            ApprovalRequest.date_to >= date_from,
            ApprovalRequest.status.in_(
                [RequestStatus.SUBMITTED, RequestStatus.APPROVED, RequestStatus.ESCALATED]
            ),
        )
    )
    if exclude_employee_id is not None:
        stmt = stmt.where(ApprovalRequest.employee_id != exclude_employee_id)

    result = await db.execute(stmt)
    rows = result.all()

    # Deduplicate by employee (one employee may have multiple overlapping requests)
    seen_employees: set[uuid.UUID] = set()
    absent_names: list[str] = []
    for req, first_name in rows:
        if req.employee_id not in seen_employees:
            seen_employees.add(req.employee_id)
            absent_names.append(first_name)

    absent_count = len(seen_employees)
    capacity_pct = (absent_count / total_employees * 100.0) if total_employees > 0 else 0.0

    return {
        "total_employees": total_employees,
        "absent_count": absent_count,
        "absent_names": absent_names,
        "capacity_pct": round(capacity_pct, 1),
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
    }


async def _living_absences_overlapping(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    employee_id: uuid.UUID,
    date_from: date,
    date_to: date,
) -> list[AbsenceEntry]:
    """Lade "lebende" Abwesenheiten im überlappenden Zeitraum als Policy-Wertobjekte.

    Reine Daten-Beschaffung — die Überschneidungs-*Entscheidung* trifft
    ``policy.absence.check_no_overlap`` (auch die Status-/Halbtag-Feinheiten).
    """
    result = await db.execute(
        select(ApprovalRequest).where(
            ApprovalRequest.tenant_id == tenant_id,
            ApprovalRequest.employee_id == employee_id,
            ApprovalRequest.date_from <= date_to,
            ApprovalRequest.date_to >= date_from,
            ApprovalRequest.status.in_(
                [RequestStatus.SUBMITTED, RequestStatus.APPROVED, RequestStatus.ESCALATED]
            ),
        )
    )
    return [
        AbsenceEntry(
            date_from=r.date_from,
            date_to=r.date_to,
            day_part=r.day_part,
            request_type=str(r.request_type),
            status=str(r.status),
            request_id=str(r.id),
        )
        for r in result.scalars().all()
    ]


async def _append_audit_log(
    db: AsyncSession,
    request_id: uuid.UUID,
    old_status: RequestStatus | None,
    new_status: RequestStatus,
    actor_id: uuid.UUID | None,
    actor_type: ActorType,
    notes: str | None = None,
) -> None:
    log = ApprovalAuditLog(
        request_id=request_id,
        old_status=old_status,
        new_status=new_status,
        actor_id=actor_id,
        actor_type=actor_type,
        notes=notes,
        created_at=datetime.now(tz=UTC),
    )
    db.add(log)
    await db.flush()
