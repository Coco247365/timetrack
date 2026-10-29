"""Stempelzeiten sind minutengenau: jede Event-Zeit wird auf die volle Minute
ABGERUNDET (11:17:34 zählt als 11:17:00). Sekunden fließen nie in die
Rechnung ein — Pausen und Arbeitszeit ergeben sich aus den gerundeten Zeiten.

Ersetzt die frühere Sekunden-Summierung über mehrere Pausen: mit
Minutenrundung gibt es keine Sekundenreste mehr, die verloren gehen könnten;
Pausen unterhalb einer Minute existieren fachlich nicht (Altsystem-Verhalten).
"""

from __future__ import annotations

from datetime import UTC, datetime

from zeiterfassung.models.clock import EventSource, EventType
from zeiterfassung.services.clock_service import recompute_daily_timesheet, record_punch


def _dt(h, m, s=0):
    return datetime(2025, 3, 10, h, m, s, tzinfo=UTC)


async def _punch_seq(db, tenant, employee, seq):
    for et, t in seq:
        await record_punch(
            db=db,
            tenant_id=tenant.id,
            employee_id=employee.id,
            event_type=et,
            source=EventSource.NFC_TERMINAL,
            event_time=t,
        )


async def test_multiple_breaks_use_floored_minutes(db, tenant, employee):
    """Sekunden werden abgerundet: 11:59:30 zählt als 11:59 → Pause 11:59–12:00 = 1 min."""
    await _punch_seq(
        db,
        tenant,
        employee,
        [
            (EventType.PUNCH_IN, _dt(11, 59)),
            (EventType.BREAK_START, _dt(11, 59, 30)),  # zählt als 11:59
            (EventType.BREAK_END, _dt(12, 0)),  # -> 1 min
            (EventType.BREAK_START, _dt(12, 8)),
            (EventType.BREAK_END, _dt(12, 10)),  # -> 2 min
            (EventType.PUNCH_OUT, _dt(12, 11)),
        ],
    )
    ts = await recompute_daily_timesheet(db, tenant.id, employee.id, _dt(12, 0).date())
    assert ts.break_minutes == 3
    # Arbeit 11:59–12:11 = 12 min brutto − 3 min Pause.
    assert ts.net_work_minutes == 9


async def test_sub_minute_breaks_round_down_to_zero(db, tenant, employee):
    """Pausen unter einer Minute innerhalb derselben Minute fallen weg (abrunden)."""
    await _punch_seq(
        db,
        tenant,
        employee,
        [
            (EventType.PUNCH_IN, _dt(8, 0)),
            (EventType.BREAK_START, _dt(9, 0)),
            (EventType.BREAK_END, _dt(9, 0, 50)),  # 09:00–09:00 -> 0 min
            (EventType.BREAK_START, _dt(10, 0)),
            (EventType.BREAK_END, _dt(10, 0, 50)),  # 0 min
            (EventType.BREAK_START, _dt(11, 0)),
            (EventType.BREAK_END, _dt(11, 0, 50)),  # 0 min
            (EventType.PUNCH_OUT, _dt(12, 0)),
        ],
    )
    ts = await recompute_daily_timesheet(db, tenant.id, employee.id, _dt(12, 0).date())
    assert ts.break_minutes == 0
    # Arbeit: volle 4 h — Sekundenreste existieren nicht.
    assert ts.net_work_minutes == 240
