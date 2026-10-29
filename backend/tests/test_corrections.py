"""Tests for append-only timesheet corrections.

Corrections never mutate the raw kiosk log: each correction is a new event that
supersedes an earlier one (time change) or voids it. The recompute projection
must ignore superseded and voided events.
"""

from __future__ import annotations

from datetime import UTC, datetime

from zeiterfassung.models.clock import EventSource, EventType
from zeiterfassung.services.clock_service import (
    add_break,
    add_manual_event,
    correct_event,
    get_day_events,
    recompute_daily_timesheet,
    record_punch,
    void_event,
)


def _dt(h: int, m: int = 0) -> datetime:
    return datetime(2025, 3, 10, h, m, tzinfo=UTC)


WORK_DATE = _dt(0).date()


class TestManualEntry:
    async def test_manual_clock_in_out_computes_work(self, db, tenant, employee) -> None:
        await add_manual_event(
            db, tenant.id, employee.id, EventType.PUNCH_IN, _dt(8), employee.id, "vergessen"
        )
        await add_manual_event(
            db, tenant.id, employee.id, EventType.PUNCH_OUT, _dt(16), employee.id, "vergessen"
        )
        ts = await recompute_daily_timesheet(db, tenant.id, employee.id, WORK_DATE)
        # 8 h ohne gestempelte Pause: gesetzliche Mindestpause (30 min) wird automatisch angesetzt.
        assert ts.break_minutes == 30
        assert ts.net_work_minutes == 8 * 60 - 30

    async def test_multiple_breaks_subtracted(self, db, tenant, employee) -> None:
        await add_manual_event(
            db, tenant.id, employee.id, EventType.PUNCH_IN, _dt(8), employee.id, None
        )
        await add_manual_event(
            db, tenant.id, employee.id, EventType.PUNCH_OUT, _dt(17), employee.id, None
        )
        # Two separate breaks, von–bis: 30 min + 15 min = 45 min total.
        await add_break(db, tenant.id, employee.id, _dt(12), _dt(12, 30), employee.id, None)
        await add_break(db, tenant.id, employee.id, _dt(15), _dt(15, 15), employee.id, None)
        ts = await recompute_daily_timesheet(db, tenant.id, employee.id, WORK_DATE)
        assert ts.break_minutes == 45
        assert ts.net_work_minutes == 9 * 60 - 45


class TestCorrection:
    async def test_correcting_punch_in_keeps_original_in_log(self, db, tenant, employee) -> None:
        pin = await record_punch(
            db,
            tenant.id,
            employee.id,
            EventType.PUNCH_IN,
            EventSource.NFC_TERMINAL,
            event_time=_dt(8, 14),
        )
        await record_punch(
            db,
            tenant.id,
            employee.id,
            EventType.PUNCH_OUT,
            EventSource.NFC_TERMINAL,
            event_time=_dt(16),
        )
        # Correct the clock-in from 08:14 to 08:00.
        await correct_event(db, tenant.id, pin.id, _dt(8), employee.id, "Karte spät erkannt")

        ts = await recompute_daily_timesheet(db, tenant.id, employee.id, WORK_DATE)
        # korrigiertes 08:00 (nicht 08:14); minus 30 min automatische Pflichtpause
        assert ts.net_work_minutes == 8 * 60 - 30

        events = await get_day_events(db, tenant.id, employee.id, WORK_DATE)
        # Raw log keeps the original; only the correction is effective.
        original = next(e for e in events if e["id"] == pin.id)
        assert original["effective"] is False
        effective_ins = [e for e in events if e["event_type"] == "PUNCH_IN" and e["effective"]]
        assert len(effective_ins) == 1
        assert effective_ins[0]["reason"] == "Karte spät erkannt"

    async def test_void_removes_events_from_computation(self, db, tenant, employee) -> None:
        # Classic "tapped out by mistake, then tapped back in": the day should be
        # one continuous 08:00–16:00 session. Void both bogus events to restore it.
        await record_punch(
            db,
            tenant.id,
            employee.id,
            EventType.PUNCH_IN,
            EventSource.NFC_TERMINAL,
            event_time=_dt(8),
        )
        bogus_out = await record_punch(
            db,
            tenant.id,
            employee.id,
            EventType.PUNCH_OUT,
            EventSource.NFC_TERMINAL,
            event_time=_dt(9),
        )
        bogus_in = await record_punch(
            db,
            tenant.id,
            employee.id,
            EventType.PUNCH_IN,
            EventSource.NFC_TERMINAL,
            event_time=_dt(9, 30),
        )
        await record_punch(
            db,
            tenant.id,
            employee.id,
            EventType.PUNCH_OUT,
            EventSource.NFC_TERMINAL,
            event_time=_dt(16),
        )
        await void_event(db, tenant.id, bogus_out.id, employee.id, "versehentlich raus")
        await void_event(db, tenant.id, bogus_in.id, employee.id, "versehentlich raus")

        ts = await recompute_daily_timesheet(db, tenant.id, employee.id, WORK_DATE)
        # one continuous session restored; minus 30 min automatische Pflichtpause
        assert ts.net_work_minutes == 8 * 60 - 30

        events = await get_day_events(db, tenant.id, employee.id, WORK_DATE)
        assert next(e for e in events if e["id"] == bogus_out.id)["effective"] is False
        assert next(e for e in events if e["id"] == bogus_in.id)["effective"] is False
