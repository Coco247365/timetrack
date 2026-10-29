"""Zustandspruefung ignoriert stornierte/ersetzte Stempel nicht mehr (#185).

Vorfall 23.07.2026: Nach dem Stornieren eines versehentlichen Kiosk-"Gehen"
lehnte das Terminal die Pause ab ("current state is 'PUNCH_OUT'"), weil
``_get_last_event`` den Storno-Marker als aktuellen Stand las.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from zeiterfassung.models.clock import EventSource, EventType, PunchEvent
from zeiterfassung.services import clock_service


def _ev(tenant, employee, *, typ, minute, **kw):
    t = datetime(2026, 7, 23, 6, minute, tzinfo=UTC)
    defaults = {
        "tenant_id": tenant.id,
        "employee_id": employee.id,
        "event_type": typ,
        "event_time": t,
        "source": EventSource.NFC_TERMINAL,
        "created_at": t,
    }
    defaults.update(kw)
    return PunchEvent(**defaults)


@pytest.mark.asyncio
async def test_pause_nach_storniertem_gehen_erlaubt(db, tenant, employee):
    """Vorfall eines Mitarbeiters: Gehen + Kommen in derselben Minute, Gehen
    storniert -> Pause muss gehen (vorher HTTP 409)."""
    gehen = _ev(tenant, employee, typ=EventType.PUNCH_OUT, minute=44)
    db.add(gehen)
    await db.flush()
    kommen = _ev(tenant, employee, typ=EventType.PUNCH_IN, minute=44)
    db.add(kommen)
    await db.flush()
    # Storno-Marker: gleiche Minute, hoechste ID — genau die Falle.
    db.add(
        _ev(
            tenant,
            employee,
            typ=EventType.PUNCH_OUT,
            minute=44,
            source=EventSource.ADMIN_MANUAL,
            is_void=True,
            corrects_event_id=gehen.id,
        )
    )
    await db.flush()

    last = await clock_service._get_last_event(db, tenant.id, employee.id)  # noqa: SLF001
    assert last is not None
    assert last.id == kommen.id, "Storno-Marker darf nicht als aktueller Stand gelten"

    ev = await clock_service.record_punch(
        db=db,
        tenant_id=tenant.id,
        employee_id=employee.id,
        event_type=EventType.BREAK_START,
        source=EventSource.NFC_TERMINAL,
    )
    assert ev.event_type == EventType.BREAK_START


@pytest.mark.asyncio
async def test_storniertes_kommen_erlaubt_keine_pause(db, tenant, employee):
    """Gegenprobe: wird das KOMMEN storniert, ist niemand eingestempelt."""
    kommen = _ev(tenant, employee, typ=EventType.PUNCH_IN, minute=10)
    db.add(kommen)
    await db.flush()
    db.add(
        _ev(
            tenant,
            employee,
            typ=EventType.PUNCH_IN,
            minute=10,
            source=EventSource.ADMIN_MANUAL,
            is_void=True,
            corrects_event_id=kommen.id,
        )
    )
    await db.flush()

    assert await clock_service._get_last_event(db, tenant.id, employee.id) is None  # noqa: SLF001
    with pytest.raises(clock_service.InvalidPunchStateError):
        await clock_service.record_punch(
            db=db,
            tenant_id=tenant.id,
            employee_id=employee.id,
            event_type=EventType.BREAK_START,
            source=EventSource.NFC_TERMINAL,
        )


@pytest.mark.asyncio
async def test_korrektur_ist_der_aktuelle_stand(db, tenant, employee):
    """Zeit-Korrektur (kein Storno) ersetzt das Original als aktueller Stand."""
    orig = _ev(tenant, employee, typ=EventType.PUNCH_IN, minute=5)
    db.add(orig)
    await db.flush()
    korrektur = _ev(
        tenant,
        employee,
        typ=EventType.PUNCH_IN,
        minute=30,
        source=EventSource.ADMIN_MANUAL,
        corrects_event_id=orig.id,
        reason="Terminal defekt",
    )
    db.add(korrektur)
    await db.flush()

    last = await clock_service._get_last_event(db, tenant.id, employee.id)  # noqa: SLF001
    assert last is not None and last.id == korrektur.id
