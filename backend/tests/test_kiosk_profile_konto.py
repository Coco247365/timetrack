"""Kiosk-Profil (#148): Ueberstunden-Konto kommt aus dem Ledger (live_balance),
nicht aus dem toten MonthlySummary-Feld (das nur seed_demo befuellt)."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from zeiterfassung.api.deps import hash_device_key
from zeiterfassung.models.device import Device
from zeiterfassung.models.overtime import OvertimeEntry


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


async def _device(db, tenant_id) -> str:
    plaintext = f"zdk_test_{uuid.uuid4().hex}"
    now = datetime.now(tz=UTC)
    db.add(
        Device(
            tenant_id=tenant_id,
            name="Terminal Test",
            key_hash=hash_device_key(plaintext),
            is_active=True,
            created_at=now,
            updated_at=now,
        )
    )
    await db.flush()
    return plaintext


class TestKioskProfilKonto:
    async def test_konto_kommt_aus_dem_ledger(self, db, client, tenant, employee):
        # Auto-Erfassung + Soll-Plan 0: weder Vergangenheits- noch der heutige
        # Tag erzeugen Minus — der Saldo ist exakt die Ledger-Korrektur (+10h),
        # unabhaengig davon, an welchem Wochentag der Test laeuft.
        employee.time_capture = "auto"
        employee.weekday_target_minutes = [0, 0, 0, 0, 0, 0, 0]
        now = datetime.now(tz=UTC)
        db.add(
            OvertimeEntry(
                tenant_id=tenant.id,
                employee_id=employee.id,
                year_month=date(2026, 3, 1),
                entry_date=date(2026, 3, 31),
                entry_type="adjustment",
                hours=Decimal("10.00"),
                reason="Anfangsbestand Test",
                created_at=now,
            )
        )
        key = await _device(db, tenant.id)
        await db.commit()

        r = await client.post(
            "/api/v1/clock/nfc/profile",
            json={"badge_id": "BADGE001"},
            headers={"X-Device-Api-Key": key, "X-Tenant-Id": str(tenant.id)},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["arbeitszeitkonto_balance_minutes"] == 600, body
