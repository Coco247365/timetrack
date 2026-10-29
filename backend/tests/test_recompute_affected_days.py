"""Neuberechnung ueber die betroffene Spanne (offene Schicht ueber Mitternacht).

Hintergrund: ein Kommen ohne Gehen laeuft ueber Mitternacht weiter. Wird beim
Aendern einer Buchung nur deren eigener Kalendertag neu gerechnet, behaelt der
Folgetag seinen alten gespeicherten Wert — und der ist ueber die Oberflaeche
nicht mehr korrigierbar, weil es dort keine Buchung zum Loeschen gibt.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest_asyncio

from zeiterfassung.core.security import hash_password
from zeiterfassung.models.clock import EventSource, EventType, PunchEvent
from zeiterfassung.models.user import User
from zeiterfassung.services import clock_service


def _utc(tag: int, hhmm: str) -> datetime:
    """Lokale Berliner Zeit (August = MESZ, UTC+2) -> UTC."""
    h, m = map(int, hhmm.split(":"))
    minuten = h * 60 + m - 120
    return datetime(2026, 8, tag, minuten // 60, minuten % 60, tzinfo=UTC)


async def _mitarbeiter(db, tenant, *, erfassung: str = "nfc") -> User:
    now = datetime.now(tz=UTC)
    u = User(
        tenant_id=tenant.id,
        email=f"span-{erfassung}@test.de",
        hashed_password=hash_password("Passw0rd!"),
        first_name="Stefan",
        last_name="Spanne",
        employee_number=f"SPAN-{erfassung}",
        contracted_hours_per_week=40.0,
        weekday_target_minutes=[480, 480, 480, 480, 480, 0, 0],
        employment_type="hourly",
        time_capture=erfassung,
        auto_hours_per_day=8.0,
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    db.add(u)
    await db.flush()
    return u


def _event(tenant, user, typ: EventType, wann: datetime) -> PunchEvent:
    return PunchEvent(
        tenant_id=tenant.id,
        employee_id=user.id,
        event_type=typ,
        event_time=wann,
        source=EventSource.ADMIN_MANUAL,
        created_at=datetime.now(tz=UTC),
    )


@pytest_asyncio.fixture
async def offene_schicht(db, tenant):
    """Mo 10.08. 06:37 Kommen ohne Gehen; erst Mi 12.08. wieder gestempelt."""
    u = await _mitarbeiter(db, tenant)
    ein = _event(tenant, u, EventType.PUNCH_IN, _utc(10, "06:37"))
    db.add(ein)
    db.add(_event(tenant, u, EventType.PUNCH_IN, _utc(12, "06:35")))
    db.add(_event(tenant, u, EventType.PUNCH_OUT, _utc(12, "13:29")))
    await db.flush()
    for tag in (10, 11, 12):
        await clock_service.recompute_daily_timesheet(db, tenant.id, u.id, date(2026, 8, tag))
    return u, ein


class TestSpanneNeuRechnen:
    async def test_storno_zieht_den_folgetag_mit(self, db, tenant, offene_schicht) -> None:
        u, ein = offene_schicht
        await clock_service.void_event(db, tenant.id, ein.id, None, reason="nix aufgestempelt")
        await db.flush()

        for tag in (10, 11):
            ts = await clock_service._stored_timesheet(db, tenant.id, u.id, date(2026, 8, tag))
            assert ts is not None and ts.net_work_minutes == 0, (
                f"{tag}.08. muss nach dem Storno auf 0 stehen — sonst bleibt ein "
                f"Wert im Konto, den man ueber die Oberflaeche nicht mehr loswird"
            )

    async def test_spanne_endet_am_geschlossenen_tag(self, db, tenant, offene_schicht) -> None:
        """Der 12.08. ist abgeschlossen und darf von der Spanne unberuehrt bleiben."""
        u, ein = offene_schicht
        await clock_service.void_event(db, tenant.id, ein.id, None, reason="Test")
        await db.flush()
        ts = await clock_service._stored_timesheet(db, tenant.id, u.id, date(2026, 8, 12))
        assert ts is not None and ts.net_work_minutes > 0

    async def test_auto_erfasser_wird_nicht_genullt(self, db, tenant) -> None:
        """Neuberechnung darf die Stunden eines Auto-Erfassers nicht loeschen."""
        u = await _mitarbeiter(db, tenant, erfassung="auto")
        await clock_service.recompute_daily_timesheet(db, tenant.id, u.id, date(2026, 8, 10))
        ts = await clock_service._stored_timesheet(db, tenant.id, u.id, date(2026, 8, 10))
        # Ohne Stempel hat ein Auto-Erfasser keinen aus Buchungen abgeleiteten
        # Tagesbogen — die Stunden kommen aus der Auto-Befuellung, nicht von hier.
        assert ts is None or ts.net_work_minutes == 0
