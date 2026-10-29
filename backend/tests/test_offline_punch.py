"""Tests for the offline-buffer support on punches.

A terminal with an offline queue re-sends punches after a network outage:
- ``client_event_id`` (generated on the device) is stored on the event so
  retries can be detected (idempotency).
- ``event_time`` from the device is kept so the punch lands at the real time,
  not the time of the retry.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from zeiterfassung.models.clock import EventSource, EventType, PunchEvent
from zeiterfassung.services.clock_service import (
    get_punch_by_client_event_id,
    record_punch,
)


def _dt(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


class TestOfflinePunch:
    async def test_client_event_id_is_stored(self, db, tenant, employee) -> None:
        event = await record_punch(
            db=db,
            tenant_id=tenant.id,
            employee_id=employee.id,
            event_type=EventType.PUNCH_IN,
            source=EventSource.NFC_TERMINAL,
            event_time=_dt(2025, 3, 10, 8, 0),
            client_event_id="kiosk-abc-001",
        )
        assert event.client_event_id == "kiosk-abc-001"

    async def test_lookup_by_client_event_id(self, db, tenant, employee) -> None:
        await record_punch(
            db=db,
            tenant_id=tenant.id,
            employee_id=employee.id,
            event_type=EventType.PUNCH_IN,
            source=EventSource.NFC_TERMINAL,
            event_time=_dt(2025, 3, 10, 8, 0),
            client_event_id="kiosk-abc-002",
        )
        found = await get_punch_by_client_event_id(db, tenant.id, "kiosk-abc-002")
        assert found is not None
        assert found.event_type == EventType.PUNCH_IN
        # Test-DB (SQLite) liefert naive Datetimes — nur den Zeitwert vergleichen.
        assert found.event_time.replace(tzinfo=UTC) == _dt(2025, 3, 10, 8, 0)

        missing = await get_punch_by_client_event_id(db, tenant.id, "does-not-exist")
        assert missing is None

    async def test_delayed_event_time_lands_on_original_day(self, db, tenant, employee) -> None:
        # Nachgereichter Stempel: event_time = gestern, gebucht heute.
        await record_punch(
            db=db,
            tenant_id=tenant.id,
            employee_id=employee.id,
            event_type=EventType.PUNCH_IN,
            source=EventSource.NFC_TERMINAL,
            event_time=_dt(2025, 3, 10, 8, 0),
            client_event_id="kiosk-late-1",
        )
        await record_punch(
            db=db,
            tenant_id=tenant.id,
            employee_id=employee.id,
            event_type=EventType.PUNCH_OUT,
            source=EventSource.NFC_TERMINAL,
            event_time=_dt(2025, 3, 10, 16, 30),
            client_event_id="kiosk-late-2",
        )
        rows = (
            (
                await db.execute(
                    select(PunchEvent).where(
                        PunchEvent.tenant_id == tenant.id,
                        PunchEvent.employee_id == employee.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert {e.event_time.date().isoformat() for e in rows} == {"2025-03-10"}

    async def test_web_punch_has_no_client_event_id(self, db, tenant, employee) -> None:
        event = await record_punch(
            db=db,
            tenant_id=tenant.id,
            employee_id=employee.id,
            event_type=EventType.PUNCH_IN,
            source=EventSource.WEB_PORTAL,
            event_time=_dt(2025, 3, 11, 8, 0),
        )
        assert event.client_event_id is None
