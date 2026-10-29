"""Automatisches Ausstempeln nach 14 h offener Schicht (Spec 2026-08-03).

- Offene Schicht > 14 h → synthetisches PUNCH_OUT bei Schichtbeginn + 14 h.
- Abgeleiteter Tag: 0 h, geflaggt „Ausstempeln vergessen", nicht saldowirksam.
- Schicht < 14 h (inkl. echte Nachtschicht) bleibt unberührt.
- HR-Korrektur (echte Endzeit) löst den Tag auf.
- Export ist gesperrt, solange ein Tag unaufgelöst ist.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from zeiterfassung.models.clock import EventSource, EventType, PunchEvent, TimesheetStatus
from zeiterfassung.services import export_service
from zeiterfassung.services.clock_service import (
    FORGOTTEN_CLOCKOUT_FLAG,
    auto_clockout_overdue_shifts,
    correct_event,
    recompute_daily_timesheet,
    record_punch,
    unresolved_auto_clockout_events,
)

_BERLIN = ZoneInfo("Europe/Berlin")


def _b(y: int, mo: int, d: int, h: int, m: int = 0) -> datetime:
    """Berliner Ortszeit als UTC-Instant (wie im Storage)."""
    return datetime(y, mo, d, h, m, tzinfo=_BERLIN).astimezone(UTC)


async def _open_shift(db, tenant, employee, start: datetime) -> None:
    await record_punch(
        db=db,
        tenant_id=tenant.id,
        employee_id=employee.id,
        event_type=EventType.PUNCH_IN,
        source=EventSource.NFC_TERMINAL,
        event_time=start,
    )


async def _auto_out(db, employee) -> PunchEvent | None:
    res = await db.execute(
        select(PunchEvent).where(
            PunchEvent.employee_id == employee.id,
            PunchEvent.source == EventSource.SYSTEM_AUTO_TIMEOUT,
            PunchEvent.event_type == EventType.PUNCH_OUT,
        )
    )
    return res.scalars().first()


async def test_open_shift_over_14h_is_auto_closed_at_start_plus_14h(db, tenant, employee):
    start = _b(2025, 3, 10, 6, 0)
    await _open_shift(db, tenant, employee, start)

    n = await auto_clockout_overdue_shifts(db, now=start + timedelta(hours=15))
    assert n == 1

    auto = await _auto_out(db, employee)
    assert auto is not None
    assert auto.event_time.astimezone(UTC) == start + timedelta(hours=14)


async def test_auto_closed_day_is_zero_and_flagged(db, tenant, employee):
    start = _b(2025, 3, 10, 6, 0)
    await _open_shift(db, tenant, employee, start)
    await auto_clockout_overdue_shifts(db, now=start + timedelta(hours=15))

    ts = await recompute_daily_timesheet(db, tenant.id, employee.id, date(2025, 3, 10))
    assert ts.net_work_minutes == 0
    assert ts.overtime_minutes == 0
    assert ts.status == TimesheetStatus.FLAGGED
    assert FORGOTTEN_CLOCKOUT_FLAG in (ts.flagged_reason or "")


async def test_shift_under_14h_untouched(db, tenant, employee):
    start = _b(2025, 3, 10, 6, 0)
    await _open_shift(db, tenant, employee, start)

    assert await auto_clockout_overdue_shifts(db, now=start + timedelta(hours=12)) == 0
    assert await _auto_out(db, employee) is None


async def test_real_night_shift_under_14h_untouched(db, tenant, employee):
    # 22:00 → offen, 6 h später (04:00): echte Nachtschicht, unter der Schwelle.
    start = _b(2025, 3, 10, 22, 0)
    await _open_shift(db, tenant, employee, start)

    assert await auto_clockout_overdue_shifts(db, now=start + timedelta(hours=6)) == 0


async def test_idempotent(db, tenant, employee):
    start = _b(2025, 3, 10, 6, 0)
    await _open_shift(db, tenant, employee, start)
    now = start + timedelta(hours=15)

    assert await auto_clockout_overdue_shifts(db, now=now) == 1
    assert await auto_clockout_overdue_shifts(db, now=now) == 0


async def test_open_break_is_closed_too(db, tenant, employee):
    start = _b(2025, 3, 10, 6, 0)
    await _open_shift(db, tenant, employee, start)
    await record_punch(
        db=db,
        tenant_id=tenant.id,
        employee_id=employee.id,
        event_type=EventType.BREAK_START,
        source=EventSource.NFC_TERMINAL,
        event_time=start + timedelta(hours=1),
    )

    assert await auto_clockout_overdue_shifts(db, now=start + timedelta(hours=15)) == 1

    res = await db.execute(
        select(PunchEvent).where(
            PunchEvent.employee_id == employee.id,
            PunchEvent.source == EventSource.SYSTEM_AUTO_TIMEOUT,
        )
    )
    assert {e.event_type for e in res.scalars().all()} == {
        EventType.BREAK_END,
        EventType.PUNCH_OUT,
    }


async def test_hr_correction_resolves_day(db, tenant, employee):
    start = _b(2025, 3, 10, 6, 0)
    await _open_shift(db, tenant, employee, start)
    await auto_clockout_overdue_shifts(db, now=start + timedelta(hours=15))
    auto = await _auto_out(db, employee)

    await correct_event(
        db,
        tenant.id,
        auto.id,
        new_time=_b(2025, 3, 10, 14, 30),
        created_by=None,
        reason="Ausstempeln nachgetragen",
    )

    ts = await recompute_daily_timesheet(db, tenant.id, employee.id, date(2025, 3, 10))
    assert ts.net_work_minutes > 0
    assert FORGOTTEN_CLOCKOUT_FLAG not in (ts.flagged_reason or "")


async def test_export_blocked_until_resolved(db, tenant, employee):
    start = _b(2025, 3, 10, 6, 0)
    await _open_shift(db, tenant, employee, start)
    await auto_clockout_overdue_shifts(db, now=start + timedelta(hours=15))

    evts = await unresolved_auto_clockout_events(
        db, tenant.id, [employee.id], _b(2025, 3, 1, 0), _b(2025, 4, 1, 0)
    )
    assert len(evts) == 1
    with pytest.raises(ValueError, match="vergessenem Ausstempeln"):
        await export_service._assert_no_forgotten_clockouts(db, tenant.id, [employee.id], 2025, 3)

    auto = await _auto_out(db, employee)
    await correct_event(
        db,
        tenant.id,
        auto.id,
        new_time=_b(2025, 3, 10, 14, 30),
        created_by=None,
        reason="Ausstempeln nachgetragen",
    )
    # Nach Auflösung kein Fehler mehr.
    await export_service._assert_no_forgotten_clockouts(db, tenant.id, [employee.id], 2025, 3)
