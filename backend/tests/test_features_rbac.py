"""Rechte fuer die Funktions-Schalter (PATCH /admin/features, ICS-Token).

Eigenes feines Recht ``admin:features``: ``admin:settings`` (z. B. Buchhaltung
fuer Lohnarten) reicht bewusst NICHT, um Firmen-Funktionen an-/abzuschalten
oder den Kalender-Abo-Link zu erneuern.
"""

from __future__ import annotations

import uuid

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from zeiterfassung.core.security import create_access_token


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


async def _headers(
    db: AsyncSession, tenant_id: uuid.UUID, permissions: list[str]
) -> dict[str, str]:
    from tests.conftest import ensure_login_account

    acc = await ensure_login_account(db, tenant_id)
    tok = create_access_token(
        user_id=str(acc.id),
        tenant_id=str(tenant_id),
        permissions=permissions,
        department_id=None,
        role_type="buchhaltung",
        data_scope="global",
    )
    return {"Authorization": f"Bearer {tok}"}


class TestFeaturesRbac:
    async def test_admin_settings_reicht_nicht_fuer_features(self, db, client, tenant) -> None:
        r = await client.patch(
            "/api/v1/admin/features",
            json={"org_chart": False},
            headers=await _headers(db, tenant.id, ["admin:settings"]),
        )
        assert r.status_code == 403, r.text

    async def test_admin_features_darf_schalten(self, db, client, tenant) -> None:
        r = await client.patch(
            "/api/v1/admin/features",
            json={"org_chart": False},
            headers=await _headers(db, tenant.id, ["admin:features"]),
        )
        assert r.status_code == 200, r.text
        assert r.json()["org_chart"] is False

    async def test_features_lesen_bleibt_fuer_alle(self, db, client, tenant) -> None:
        # Navigation braucht die Schalter — lesen darf jeder Eingeloggte.
        r = await client.get(
            "/api/v1/admin/features",
            headers=await _headers(db, tenant.id, ["timesheet:view_own"]),
        )
        assert r.status_code == 200, r.text

    async def test_ics_rotate_nur_mit_admin_features(self, db, client, tenant) -> None:
        # tenant muss committet sein, bevor der erste (403-)Request laeuft: seit
        # login-hardening fuehrt get_current_user selbst fuer eine spaeter mit
        # 403 abgelehnte Anfrage schon eine echte DB-Query aus (Account-Lookup),
        # was auf der geteilten SQLite-Testverbindung eine Transaktion oeffnet;
        # deren Rollback beim Session-Close (kein Endpoint-Commit im 403-Pfad)
        # wuerde sonst den nur geflushten, nie committeten Tenant-Fixture-Datensatz
        # mit wegspuelen und der zweite Request faende ihn nicht mehr (404).
        await db.commit()
        r = await client.post(
            "/api/v1/admin/ics-feed/rotate",
            headers=await _headers(db, tenant.id, ["admin:settings"]),
        )
        assert r.status_code == 403, r.text
        r = await client.post(
            "/api/v1/admin/ics-feed/rotate",
            headers=await _headers(db, tenant.id, ["admin:features"]),
        )
        assert r.status_code == 200, r.text

    async def test_ics_url_lesen_mit_settings_oder_features(self, db, client, tenant) -> None:
        # Anzeigen der Abo-URL bleibt mit admin:settings moeglich (nur nicht erneuern).
        for perms in (["admin:settings"], ["admin:features"]):
            r = await client.get(
                "/api/v1/admin/ics-feed", headers=await _headers(db, tenant.id, perms)
            )
            assert r.status_code == 200, (perms, r.text)
        r = await client.get(
            "/api/v1/admin/ics-feed", headers=await _headers(db, tenant.id, ["timesheet:view_own"])
        )
        assert r.status_code == 403, r.text
