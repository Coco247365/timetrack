"""Auto-Erfassung: an jedem Werktag ab Erfassungsbeginn bis heute das Tages-Soll."""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from zeiterfassung.models.absence import (
    ApprovalRequest,
    DayPart,
    RequestStatus,
    RequestType,
)
from zeiterfassung.models.clock import DailyTimesheet, TimesheetStatus
from zeiterfassung.services import clock_service
from zeiterfassung.services import overtime_service as ot

NOW = datetime(2026, 1, 1, tzinfo=UTC)


async def _dates(db, tenant, employee):
    rows = (
        await db.execute(
            select(DailyTimesheet.work_date, DailyTimesheet.net_work_minutes).where(
                DailyTimesheet.tenant_id == tenant.id,
                DailyTimesheet.employee_id == employee.id,
            )
        )
    ).all()
    return {d.isoformat(): n for d, n in rows}


@pytest.mark.asyncio
async def test_auto_fills_workdays_up_to_today_not_future(db, tenant, employee):
    employee.time_capture = "auto"
    employee.counting_start_date = date(2026, 7, 1)
    await db.flush()

    # Monatssicht Juli; "heute" = 10.07.
    await clock_service.ensure_auto_timesheets(
        db, tenant.id, employee.id, date(2026, 7, 1), date(2026, 7, 31), today=date(2026, 7, 10)
    )
    got = await _dates(db, tenant, employee)

    # Werktage 1.–10.7. (ohne Sa 4. / So 5.) → 8 Tage à 8h (480 min)
    assert set(got) == {
        "2026-07-01",
        "2026-07-02",
        "2026-07-03",
        "2026-07-06",
        "2026-07-07",
        "2026-07-08",
        "2026-07-09",
        "2026-07-10",
    }
    assert all(v == 480 for v in got.values())
    # Zukunft (11.7.+) und Wochenende (4./5.7.) NICHT angelegt
    assert "2026-07-11" not in got and "2026-07-04" not in got


@pytest.mark.asyncio
async def test_auto_skips_absence_and_is_idempotent(db, tenant, employee):
    employee.time_capture = "auto"
    employee.counting_start_date = date(2026, 7, 1)
    db.add(
        ApprovalRequest(
            tenant_id=tenant.id,
            employee_id=employee.id,
            request_type=RequestType.VACATION,
            status=RequestStatus.APPROVED,
            date_from=date(2026, 7, 7),
            date_to=date(2026, 7, 7),
            day_part=DayPart.FULL,
            days_count=Decimal("1"),
        )
    )
    await db.flush()

    for _ in range(2):  # zweimal aufrufen → idempotent
        await clock_service.ensure_auto_timesheets(
            db, tenant.id, employee.id, date(2026, 7, 1), date(2026, 7, 31), today=date(2026, 7, 10)
        )
    got = await _dates(db, tenant, employee)

    assert "2026-07-07" not in got, "Urlaubstag darf nicht auto-gefüllt werden"
    assert len(got) == 7  # 8 Werktage minus 1 Urlaubstag, keine Duplikate


@pytest.mark.asyncio
async def test_auto_day_has_start_break_end(db, tenant, employee):
    """Auto-Tag: 07:00 Start, Pflichtpause 30 min (8h > 6h), Ende = Start + Soll + Pause."""
    employee.time_capture = "auto"
    employee.counting_start_date = date(2026, 7, 1)
    await db.flush()
    await clock_service.ensure_auto_timesheets(
        db, tenant.id, employee.id, date(2026, 7, 1), date(2026, 7, 3), today=date(2026, 7, 3)
    )
    row = (
        await db.execute(
            select(DailyTimesheet).where(
                DailyTimesheet.employee_id == employee.id,
                DailyTimesheet.work_date == date(2026, 7, 2),
            )
        )
    ).scalar_one()
    assert row.net_work_minutes == 480
    assert row.break_minutes == 30
    assert row.gross_work_minutes == 510  # 8h + 30min
    assert row.start_time is not None and row.end_time is not None


@pytest.mark.asyncio
async def test_old_auto_row_gets_upgraded(db, tenant, employee):
    """Ein alter Auto-Tagesbogen (ohne Start/Pause) wird auf das kanonische Format nachgezogen."""
    employee.time_capture = "auto"
    employee.counting_start_date = date(2026, 7, 1)
    db.add(
        DailyTimesheet(
            tenant_id=tenant.id,
            employee_id=employee.id,
            work_date=date(2026, 7, 2),
            start_time=None,
            break_minutes=0,
            net_work_minutes=480,
            gross_work_minutes=480,
            overtime_minutes=0,
            status=TimesheetStatus.COMPLETE,
            computed_from_events=False,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    await db.flush()
    await clock_service.ensure_auto_timesheets(
        db, tenant.id, employee.id, date(2026, 7, 1), date(2026, 7, 3), today=date(2026, 7, 3)
    )
    row = (
        await db.execute(
            select(DailyTimesheet).where(
                DailyTimesheet.employee_id == employee.id,
                DailyTimesheet.work_date == date(2026, 7, 2),
            )
        )
    ).scalar_one()  # kein Duplikat
    assert row.break_minutes == 30 and row.gross_work_minutes == 510
    assert row.start_time is not None


@pytest.mark.asyncio
async def test_set_manual_day_computes_net_and_upserts(db, tenant, employee):
    """Tageweise setzen: net = (Geht − Kommt) − Pause.

    Überschreibt denselben Tag (kein Duplikat).
    """
    employee.time_capture = "auto"
    await db.flush()
    ts = await clock_service.set_manual_day(
        db, tenant.id, employee.id, date(2026, 7, 2), "07:00", "16:00", 30
    )
    assert ts.gross_work_minutes == 540 and ts.break_minutes == 30
    assert ts.net_work_minutes == 510  # 9h − 30min

    ts2 = await clock_service.set_manual_day(
        db, tenant.id, employee.id, date(2026, 7, 2), "07:00", "15:00", 30
    )
    assert ts2.net_work_minutes == 450  # 8h − 30min

    n = (
        await db.execute(
            select(func.count())
            .select_from(DailyTimesheet)
            .where(
                DailyTimesheet.employee_id == employee.id,
                DailyTimesheet.work_date == date(2026, 7, 2),
            )
        )
    ).scalar()
    assert n == 1

    with pytest.raises(ValueError):
        await clock_service.set_manual_day(
            db, tenant.id, employee.id, date(2026, 7, 2), "16:00", "07:00", 0
        )


@pytest.mark.asyncio
async def test_unworked_day_is_unexcused_and_not_refilled(db, tenant, employee):
    """'Nicht gearbeitet' → leerer Tagesbogen: zaehlt als unentschuldigt (−8h) und wird
    vom Auto-Fill NICHT ueberschrieben."""
    employee.time_capture = "auto"
    employee.counting_start_date = date(2026, 7, 1)
    await db.flush()

    await clock_service.set_day_unworked(db, tenant.id, employee.id, date(2026, 7, 2))
    await clock_service.ensure_auto_timesheets(
        db, tenant.id, employee.id, date(2026, 7, 1), date(2026, 7, 3), today=date(2026, 7, 3)
    )
    row = (
        await db.execute(
            select(DailyTimesheet).where(
                DailyTimesheet.employee_id == employee.id,
                DailyTimesheet.work_date == date(2026, 7, 2),
            )
        )
    ).scalar_one()
    assert row.net_work_minutes == 0 and row.start_time is None  # bleibt leer

    net = await ot._net_overtime_minutes_for_month(
        db, tenant.id, employee, 2026, 7, today=date(2026, 7, 3)
    )
    assert net == -480, f"nicht gearbeitet = −480 min (01./03. je 0), war {net}"


@pytest.mark.asyncio
async def test_unworked_day_overridden_by_vacation(db, tenant, employee):
    """Urlaub deckt einen 'nicht gearbeitet'-Tag ab → kein Minus mehr."""
    employee.time_capture = "auto"
    employee.counting_start_date = date(2026, 7, 1)
    await db.flush()
    await clock_service.set_day_unworked(db, tenant.id, employee.id, date(2026, 7, 2))
    db.add(
        ApprovalRequest(
            tenant_id=tenant.id,
            employee_id=employee.id,
            request_type=RequestType.VACATION,
            status=RequestStatus.APPROVED,
            date_from=date(2026, 7, 2),
            date_to=date(2026, 7, 2),
            day_part=DayPart.FULL,
            days_count=Decimal("1"),
            created_at=NOW,
            updated_at=NOW,
        )
    )
    await db.flush()

    net = await ot._net_overtime_minutes_for_month(
        db, tenant.id, employee, 2026, 7, today=date(2026, 7, 3)
    )
    assert net == 0, f"Urlaub ueberschreibt 'nicht gearbeitet', war {net}"
    rows = await ot.daily_breakdown(db, tenant.id, employee, today=date(2026, 7, 3))
    assert not any(r.get("absent") for r in rows), "kein unentschuldigter Tag mehr"


@pytest.mark.asyncio
async def test_half_vacation_on_unworked_day_is_not_unexcused(db, tenant, employee):
    """Halber Urlaub auf einem 'nicht gearbeitet'-Tag.

    KEIN Unentschuldigt, nur −4h (ungedeckte Hälfte).
    """
    employee.time_capture = "auto"
    employee.counting_start_date = date(2026, 7, 1)
    await db.flush()
    await clock_service.set_day_unworked(db, tenant.id, employee.id, date(2026, 7, 2))
    db.add(
        ApprovalRequest(
            tenant_id=tenant.id,
            employee_id=employee.id,
            request_type=RequestType.VACATION,
            status=RequestStatus.APPROVED,
            date_from=date(2026, 7, 2),
            date_to=date(2026, 7, 2),
            day_part=DayPart.AM,
            days_count=Decimal("0.5"),
            created_at=NOW,
            updated_at=NOW,
        )
    )
    await db.flush()

    rows = await ot.daily_breakdown(db, tenant.id, employee, today=date(2026, 7, 3))
    day = next(r for r in rows if r["date"] == "2026-07-02")
    assert day["absent"] is False, "halb gedeckt → nicht unentschuldigt"
    assert day["delta_hours"] == -4.0, "nur die ungedeckte Haelfte als Minus"
    assert 0 < day["absence_fraction"] < 1


@pytest.mark.asyncio
async def test_set_manual_day_blocked_on_sick_day(db, tenant, employee):
    """An Krankheitstagen darf keine Arbeitszeit gesetzt werden."""
    employee.time_capture = "auto"
    db.add(
        ApprovalRequest(
            tenant_id=tenant.id,
            employee_id=employee.id,
            request_type=RequestType.SICK_LEAVE,
            status=RequestStatus.APPROVED,
            date_from=date(2026, 7, 2),
            date_to=date(2026, 7, 2),
            day_part=DayPart.FULL,
            days_count=Decimal("1"),
            created_at=NOW,
            updated_at=NOW,
        )
    )
    await db.flush()
    with pytest.raises(clock_service.InvalidPunchStateError):
        await clock_service.set_manual_day(
            db, tenant.id, employee.id, date(2026, 7, 2), "07:00", "15:00", 30
        )


@pytest.mark.asyncio
async def test_auto_capture_overtime_stays_zero(db, tenant, employee):
    """Auto-Tage = Tages-Soll → Überstundensaldo bleibt 0 (Ist = Soll)."""
    employee.time_capture = "auto"
    employee.counting_start_date = date(2026, 7, 1)
    await db.flush()
    await clock_service.ensure_auto_timesheets(
        db, tenant.id, employee.id, date(2026, 7, 1), date(2026, 7, 31), today=date(2026, 7, 10)
    )
    net = await ot._net_overtime_minutes_for_month(
        db, tenant.id, employee, 2026, 7, today=date(2026, 7, 10)
    )
    assert net == 0, f"Auto-Erfassung: Saldo muss 0 sein, war {net}"


@pytest.mark.asyncio
async def test_recompute_laesst_manuellen_tag_stehen(db, tenant, employee):
    """Regression: Tagesdetail zeigte 0h 00m fuer manuell gesetzte Tage.

    recompute_daily_timesheet baut den Bogen aus punch_events neu — ein
    manuell gesetzter Tag (computed_from_events=False) hat keine Events und
    wurde auf 0 h "nachgerechnet" (Kalenderzelle 4h aus dem Bogen, Detail 0h
    aus dem Rebuild). Ohne wirksame Events muss der manuelle Bogen stehen
    bleiben; mit echten Stempeln gewinnen die Events wie ueberall.
    """
    from zeiterfassung.models.clock import EventSource, EventType, PunchEvent

    ts = await clock_service.set_manual_day(
        db, tenant.id, employee.id, date(2026, 7, 2), "07:00", "11:30", 30
    )
    assert ts.net_work_minutes == 240 and ts.computed_from_events is False

    # Keine Events -> der manuelle Bogen ist die Wahrheit und bleibt unangetastet.
    re = await clock_service.recompute_daily_timesheet(db, tenant.id, employee.id, date(2026, 7, 2))
    assert re.net_work_minutes == 240
    assert re.break_minutes == 30
    assert re.computed_from_events is False
    assert re.status == TimesheetStatus.COMPLETE

    # Sobald echte Stempel existieren, rechnet der Rebuild aus den Events.
    for etype, hh in ((EventType.PUNCH_IN, 6), (EventType.PUNCH_OUT, 14)):
        db.add(
            PunchEvent(
                tenant_id=tenant.id,
                employee_id=employee.id,
                event_type=etype,
                event_time=datetime(2026, 7, 2, hh, 0, tzinfo=UTC),
                source=EventSource.NFC_TERMINAL,
                created_at=NOW,
            )
        )
    await db.flush()
    re2 = await clock_service.recompute_daily_timesheet(
        db, tenant.id, employee.id, date(2026, 7, 2)
    )
    assert re2.computed_from_events is True
    assert re2.gross_work_minutes == 480  # 06-14 UTC aus den Stempeln


@pytest.mark.asyncio
async def test_recompute_laesst_nicht_gearbeitet_marker_stehen(db, tenant, employee):
    """Auch der leere Marker („nicht gearbeitet", net=0) uebersteht den Rebuild."""
    ts = await clock_service.set_day_unworked(db, tenant.id, employee.id, date(2026, 7, 3))
    assert ts.net_work_minutes == 0 and ts.computed_from_events is False

    re = await clock_service.recompute_daily_timesheet(db, tenant.id, employee.id, date(2026, 7, 3))
    assert re.computed_from_events is False
    assert re.net_work_minutes == 0
    assert re.id == ts.id  # dieselbe Zeile, nichts neu angelegt


@pytest.mark.asyncio
async def test_manuelle_tag_aktionen_schreiben_audit(db, tenant, employee):
    """set_manual_day und set_day_unworked hinterlassen eine alt->neu-Spur."""
    from zeiterfassung.models.audit import AuditLog

    await clock_service.set_manual_day(
        db, tenant.id, employee.id, date(2026, 7, 8), "07:00", "15:30", 30, actor_id=employee.id
    )
    await clock_service.set_day_unworked(
        db, tenant.id, employee.id, date(2026, 7, 8), actor_id=employee.id
    )
    q = select(AuditLog.action, AuditLog.details).where(AuditLog.tenant_id == tenant.id)
    actions = (await db.execute(q)).all()
    by_action = {a: d for a, d in actions}
    assert "timesheet.manual_day_set" in by_action
    assert by_action["timesheet.manual_day_set"]["alt"] is None  # neu angelegt
    assert by_action["timesheet.manual_day_set"]["neu"]["net_work_minutes"] == 480
    assert "timesheet.day_unworked_set" in by_action
    assert by_action["timesheet.day_unworked_set"]["alt"]["net_work_minutes"] == 480
