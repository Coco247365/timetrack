"""Manuelle Stempelzeiten müssen plausibel sein: Kommt vor Geht."""

from datetime import UTC, datetime

import pytest

from zeiterfassung.models.clock import EventType
from zeiterfassung.services import clock_service


def _t(h: int, m: int) -> datetime:
    return datetime(2026, 7, 8, h, m, tzinfo=UTC)


@pytest.mark.asyncio
async def test_geht_before_kommt_rejected(db, tenant, employee):
    await clock_service.add_manual_event(
        db, tenant.id, employee.id, EventType.PUNCH_IN, _t(8, 0), None
    )
    with pytest.raises(clock_service.InvalidPunchStateError):
        await clock_service.add_manual_event(
            db, tenant.id, employee.id, EventType.PUNCH_OUT, _t(7, 0), None
        )


@pytest.mark.asyncio
async def test_kommt_before_geht_ok(db, tenant, employee):
    await clock_service.add_manual_event(
        db, tenant.id, employee.id, EventType.PUNCH_IN, _t(8, 0), None
    )
    ev = await clock_service.add_manual_event(
        db, tenant.id, employee.id, EventType.PUNCH_OUT, _t(17, 0), None
    )
    assert ev is not None


@pytest.mark.asyncio
async def test_break_covering_whole_shift_rejected(db, tenant, employee):
    await clock_service.add_manual_event(
        db, tenant.id, employee.id, EventType.PUNCH_IN, _t(8, 0), None
    )
    await clock_service.add_manual_event(
        db, tenant.id, employee.id, EventType.PUNCH_OUT, _t(9, 0), None
    )
    with pytest.raises(clock_service.InvalidPunchStateError):
        await clock_service.add_break(db, tenant.id, employee.id, _t(8, 0), _t(9, 0), None)


@pytest.mark.asyncio
async def test_editing_break_outside_window_rejected(db, tenant, employee):
    """Bypass-Fix: eine gültige Pause NACHTRÄGLICH aus dem Arbeitsfenster schieben
    (Korrektur) muss ebenfalls von der Pausen-Policy abgelehnt werden."""
    await clock_service.add_manual_event(
        db, tenant.id, employee.id, EventType.PUNCH_IN, _t(8, 0), None
    )
    await clock_service.add_manual_event(
        db, tenant.id, employee.id, EventType.PUNCH_OUT, _t(17, 0), None
    )
    start_ev, _ = await clock_service.add_break(
        db, tenant.id, employee.id, _t(12, 0), _t(12, 30), None
    )
    # Pausenbeginn vor das Kommt (08:00) ziehen → außerhalb der Arbeitszeit.
    with pytest.raises(clock_service.InvalidPunchStateError):
        await clock_service.correct_event(db, tenant.id, start_ev.id, _t(7, 0), None)


@pytest.mark.asyncio
async def test_break_on_open_shift_then_geht_leaving_zero_rejected(db, tenant, employee):
    await clock_service.add_manual_event(
        db, tenant.id, employee.id, EventType.PUNCH_IN, _t(8, 0), None
    )
    # Pause auf offener Schicht ist erlaubt …
    await clock_service.add_break(db, tenant.id, employee.id, _t(8, 0), _t(9, 0), None)
    # … aber das Geht, das 0 h Arbeit übrig lässt, wird abgelehnt.
    with pytest.raises(clock_service.InvalidPunchStateError):
        await clock_service.add_manual_event(
            db, tenant.id, employee.id, EventType.PUNCH_OUT, _t(9, 0), None
        )
