"""Tagesansicht, Reihenfolge-Pruefung und Pausenfenster nutzen den LOKALEN Tag.

Ein Stempel um 00:30 Lokalzeit (CEST = 22:30 UTC am Vortag) gehoert zum
lokalen Tag — wie im Tagesbogen. Vorher rechneten get_day_events,
_assert_punch_order und _day_window_and_breaks mit dem UTC-Tag: der Stempel
erschien in der Ansicht des Vortags, und ein korrektes Geht 06:15 wurde mit
"Kommt muss vor Geht liegen" abgelehnt (Nachtschicht-Eingabe, Spec 2026-09-15).
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from zeiterfassung.models.clock import EventType
from zeiterfassung.services import clock_service


@pytest.mark.asyncio
async def test_stempel_nach_mitternacht_gehoeren_zum_lokalen_tag(db, tenant, employee) -> None:
    # Mi 09.09.2026 00:00 lokal = Di 08.09. 22:00 UTC (CEST).
    await clock_service.add_manual_event(
        db,
        tenant.id,
        employee.id,
        EventType.PUNCH_IN,
        datetime(2026, 9, 8, 22, 0, tzinfo=UTC),
        None,
        "Nachtschicht",
    )
    # Geht 06:15 lokal = 04:15 UTC am 09.09. — muss akzeptiert werden (Kommt liegt davor).
    await clock_service.add_manual_event(
        db,
        tenant.id,
        employee.id,
        EventType.PUNCH_OUT,
        datetime(2026, 9, 9, 4, 15, tzinfo=UTC),
        None,
        "Nachtschicht",
    )
    await db.flush()

    mi = await clock_service.get_day_events(db, tenant.id, employee.id, date(2026, 9, 9))
    di = await clock_service.get_day_events(db, tenant.id, employee.id, date(2026, 9, 8))
    assert [e["event_type"] for e in mi] == ["PUNCH_IN", "PUNCH_OUT"]
    assert di == []

    # Pausenfenster des lokalen Tags sieht Kommt UND Geht.
    start, end, _ = await clock_service._day_window_and_breaks(
        db, tenant.id, employee.id, date(2026, 9, 9)
    )
    assert start is not None and end is not None

    # Tagesbogen: 6:15 Anwesenheit -> 45 min Betriebspause (ab 28.08.) -> 5:30.
    ts = await clock_service.recompute_daily_timesheet(db, tenant.id, employee.id, date(2026, 9, 9))
    assert ts.net_work_minutes == 330


@pytest.mark.asyncio
async def test_reihenfolge_pruefung_bleibt_streng(db, tenant, employee) -> None:
    await clock_service.add_manual_event(
        db,
        tenant.id,
        employee.id,
        EventType.PUNCH_IN,
        datetime(2026, 9, 9, 6, 0, tzinfo=UTC),
        None,
        "Test",
    )
    with pytest.raises(clock_service.InvalidPunchStateError):
        await clock_service.add_manual_event(
            db,
            tenant.id,
            employee.id,
            EventType.PUNCH_OUT,
            datetime(2026, 9, 9, 5, 0, tzinfo=UTC),
            None,
            "Geht vor Kommt",
        )
