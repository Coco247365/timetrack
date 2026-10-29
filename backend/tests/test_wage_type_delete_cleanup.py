"""Loeschen einer Lohnart raeumt Zuordnungen auf (Standard + Profile).

Vorher blieben tote Verweise in tenant.settings stehen — der Profil-Editor
zeigte dann leere Dropdowns statt "nicht zugeordnet".
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from zeiterfassung.core.security import create_access_token
from zeiterfassung.models.export import WageTypeMapping
from zeiterfassung.models.tenant import Tenant


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


async def _admin_headers(db: AsyncSession, tenant_id: uuid.UUID) -> dict[str, str]:
    from tests.conftest import ensure_login_account

    acc = await ensure_login_account(db, tenant_id)
    tok = create_access_token(
        user_id=str(acc.id),
        tenant_id=str(tenant_id),
        permissions=["admin:settings"],
        department_id=None,
        role_type="geschaeftsfuehrung",
        data_scope="global",
    )
    return {"Authorization": f"Bearer {tok}"}


async def _seed(db, tenant: Tenant) -> None:
    now = datetime.now(UTC)
    for nr in (153, 154):
        db.add(
            WageTypeMapping(
                tenant_id=tenant.id,
                internal_code=f"LA_{nr:04d}",
                datev_lohnart=nr,
                description=f"Test {nr}",
                is_active=True,
                unit="hours",
                created_at=now,
            )
        )
    tenant.settings = {
        **(tenant.settings or {}),
        "lohnart_zuordnung": {"KRANK_STD": "LA_0153", "KRANK_OHNE_AU_MIT_LFZ": "LA_0154"},
        "wage_profiles": [
            {"id": "p1", "name": "Schicht", "mapping": {"KRANK_STD": "LA_0153"}},
        ],
    }
    await db.commit()


class TestDeleteCleanup:
    async def test_delete_raeumt_standard_und_profile_auf(self, db, client, tenant) -> None:
        await _seed(db, tenant)
        tid = tenant.id
        r = await client.delete(
            "/api/v1/admin/wage-types/LA_0153", headers=await _admin_headers(db, tid)
        )
        assert r.status_code == 204, r.text

        # Direkt in den Settings nachsehen: LA_0153 ueberall entfernt, 154 bleibt.
        db.expire_all()
        t = (await db.execute(select(Tenant).where(Tenant.id == tid))).scalar_one()
        assert t.settings["lohnart_zuordnung"] == {"KRANK_OHNE_AU_MIT_LFZ": "LA_0154"}
        assert t.settings["wage_profiles"][0]["mapping"] == {}

    async def test_profiles_get_filtert_verwaiste_codes(self, db, client, tenant) -> None:
        """Bestandsdaten: toter Verweis in den Settings -> GET liefert ihn nicht mehr."""
        await _seed(db, tenant)
        # Toten Verweis simulieren (Lohnart existiert nicht im Katalog).
        t = (await db.execute(select(Tenant).where(Tenant.id == tenant.id))).scalar_one()
        t.settings = {
            **t.settings,
            "lohnart_zuordnung": {**t.settings["lohnart_zuordnung"], "URLAUB_STD": "LA_9999"},
        }
        await db.commit()

        r = await client.get(
            "/api/v1/admin/wage-profiles", headers=await _admin_headers(db, tenant.id)
        )
        assert r.status_code == 200, r.text
        standard = next(p for p in r.json()["profiles"] if p["id"] == "standard")
        assert "URLAUB_STD" not in standard["mapping"]  # verwaist -> ausgefiltert
        assert standard["mapping"]["KRANK_STD"] == "LA_0153"  # gueltig -> bleibt
