"""Teamkalender: Warnsymbol bei vergessenem Ausstempeln (Spec 2026-08-03).

Der Kalender liefert unaufgeloeste „Ausstempeln vergessen"-Tage als
``flagged``-Liste — aber nur an Rollen, die Arbeitszeiten ohnehin sehen
duerfen. Nach der HR-Korrektur verschwindet der Eintrag wieder.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from zeiterfassung.core.security import create_access_token
from zeiterfassung.models.clock import EventSource, EventType, PunchEvent
from zeiterfassung.services.clock_service import (
    auto_clockout_overdue_shifts,
    correct_event,
    record_punch,
)

_BERLIN = ZoneInfo("Europe/Berlin")
_START = datetime(2025, 3, 10, 6, 0, tzinfo=_BERLIN).astimezone(UTC)


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


async def _forget_clockout(db, tenant, employee) -> None:
    """Offene Schicht anlegen und vom Auto-Timeout kappen lassen."""
    await record_punch(
        db=db,
        tenant_id=tenant.id,
        employee_id=employee.id,
        event_type=EventType.PUNCH_IN,
        source=EventSource.NFC_TERMINAL,
        event_time=_START,
    )
    await auto_clockout_overdue_shifts(db, now=_START + timedelta(hours=15))
    await db.commit()


_URL = "/api/v1/absences/team-calendar?date_from=2025-03-01&date_to=2025-03-31"


async def test_flagged_day_is_reported(db, client, tenant, employee) -> None:
    await _forget_clockout(db, tenant, employee)
    hdrs = await _headers(db, tenant.id, ["calendar:view_all", "timesheet:view_all"])
    await db.commit()

    resp = await client.get(_URL, headers=hdrs)
    assert resp.status_code == 200
    flagged = resp.json()["flagged"]
    assert [f["date"] for f in flagged] == ["2025-03-10"]
    assert flagged[0]["employee_id"] == str(employee.id)


async def test_flag_disappears_after_hr_correction(db, client, tenant, employee) -> None:
    await _forget_clockout(db, tenant, employee)
    auto = (
        (
            await db.execute(
                select(PunchEvent).where(
                    PunchEvent.employee_id == employee.id,
                    PunchEvent.source == EventSource.SYSTEM_AUTO_TIMEOUT,
                    PunchEvent.event_type == EventType.PUNCH_OUT,
                )
            )
        )
        .scalars()
        .first()
    )
    await correct_event(
        db,
        tenant.id,
        auto.id,
        new_time=datetime(2025, 3, 10, 14, 30, tzinfo=_BERLIN).astimezone(UTC),
        created_by=None,
        reason="Ausstempeln nachgetragen",
    )
    hdrs = await _headers(db, tenant.id, ["calendar:view_all", "timesheet:view_all"])
    await db.commit()

    resp = await client.get(_URL, headers=hdrs)
    assert resp.json()["flagged"] == []


async def test_plain_employee_gets_no_flags(db, client, tenant, employee) -> None:
    """Ohne Arbeitszeit-Recht keine Flags — der Teamkalender bleibt zeitfrei."""
    await _forget_clockout(db, tenant, employee)
    hdrs = await _headers(db, tenant.id, ["calendar:view_all"])
    await db.commit()

    resp = await client.get(_URL, headers=hdrs)
    assert resp.status_code == 200
    assert resp.json()["flagged"] == []
