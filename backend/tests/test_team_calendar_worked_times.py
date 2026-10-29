"""Teamkalender: Beginn/Ende der Arbeitstage + Erfassungsmodus im Tooltip.

Der Kalender liefert je erfasstem Arbeitstag ``start``/``end`` (fuer
„gearbeitet von … bis …") und je Mitarbeiter ``time_capture`` (fuer den
„(auto)"-Marker) — aber nur an Rollen, die Arbeitszeiten ohnehin sehen
duerfen. Einfache Mitarbeiter sehen weiterhin nur, DASS gearbeitet wurde.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from zeiterfassung.core.security import create_access_token
from zeiterfassung.models.clock import EventSource, EventType
from zeiterfassung.services.clock_service import record_punch

_BERLIN = ZoneInfo("Europe/Berlin")
_TAG = "2025-03-10"
_START = datetime(2025, 3, 10, 6, 15, tzinfo=_BERLIN).astimezone(UTC)
_URL = "/api/v1/absences/team-calendar?date_from=2025-03-01&date_to=2025-03-31"


@pytest_asyncio.fixture
async def app_with_db(engine):
    from zeiterfassung.api import deps
    from zeiterfassung.main import create_app

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async def override_get_db():
        async with factory() as session:
            yield session

    app = create_app()
    app.dependency_overrides[deps.get_db] = override_get_db
    yield app
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client(app_with_db):
    async with AsyncClient(transport=ASGITransport(app=app_with_db), base_url="http://test") as c:
        yield c


async def _headers(db: AsyncSession, tenant_id: uuid.UUID, permissions: list[str]) -> dict:
    from tests.conftest import ensure_login_account

    acc = await ensure_login_account(db, tenant_id)
    tok = create_access_token(
        user_id=str(acc.id),
        tenant_id=str(tenant_id),
        permissions=permissions,
        data_scope="global",
    )
    return {"Authorization": f"Bearer {tok}"}


async def _arbeitstag(db, tenant, employee) -> None:
    """Ein abgeschlossener Arbeitstag: 06:15 rein, 14:30 raus."""
    for event_type, when in (
        (EventType.PUNCH_IN, _START),
        (EventType.PUNCH_OUT, _START + timedelta(hours=8, minutes=15)),
    ):
        await record_punch(
            db=db,
            tenant_id=tenant.id,
            employee_id=employee.id,
            event_type=event_type,
            source=EventSource.NFC_TERMINAL,
            event_time=when,
        )
    await db.commit()


async def test_zeiten_fuer_rolle_mit_arbeitszeit_recht(db, client, tenant, employee) -> None:
    await _arbeitstag(db, tenant, employee)
    hdrs = await _headers(db, tenant.id, ["calendar:view_all", "timesheet:view_all"])
    await db.commit()

    data = (await client.get(_URL, headers=hdrs)).json()
    tag = next(w for w in data["worked"] if w["date"] == _TAG)
    assert tag["start"] is not None and tag["end"] is not None
    # Lokale Uhrzeit des Stempels — der Tooltip zeigt "von 06:15 bis 14:30".
    assert datetime.fromisoformat(tag["start"]).astimezone(_BERLIN).strftime("%H:%M") == "06:15"
    assert datetime.fromisoformat(tag["end"]).astimezone(_BERLIN).strftime("%H:%M") == "14:30"
    # Erfassungsmodus fuer den "(auto)"-Marker.
    assert data["employees"][0]["time_capture"] == employee.time_capture


async def test_einfacher_mitarbeiter_sieht_keine_zeiten(db, client, tenant, employee) -> None:
    """Ohne Arbeitszeit-Recht: Tag ja, Uhrzeiten und Erfassungsmodus nein."""
    await _arbeitstag(db, tenant, employee)
    hdrs = await _headers(db, tenant.id, ["calendar:view_all"])
    await db.commit()

    data = (await client.get(_URL, headers=hdrs)).json()
    tag = next(w for w in data["worked"] if w["date"] == _TAG)
    assert "start" not in tag and "end" not in tag
    assert "time_capture" not in data["employees"][0]
