"""Bereiche sind firmenunabhaengig — reines Mitarbeiter-Attribut (#183).

Loest die Firmenbindung aus Spec 2026-07-14 ab: die beiden Firmen sind
operativ ein Betrieb, ein Bereich umfasst die Leute beider Firmen.
"""

from __future__ import annotations

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


async def _headers(db, tenant_id) -> dict[str, str]:
    from tests.conftest import ensure_login_account

    acc = await ensure_login_account(db, tenant_id)
    tok = create_access_token(
        user_id=str(acc.id),
        tenant_id=str(tenant_id),
        permissions=["admin:settings", "users:write", "users:read_all", "admin:dashboard"],
        department_id=None,
        role_type="geschaeftsfuehrung",
        data_scope="global",
    )
    return {"Authorization": f"Bearer {tok}"}


async def _company(db, tenant, name: str, active: bool = True):
    from datetime import UTC, datetime

    from zeiterfassung.models.company import Company

    now = datetime.now(tz=UTC)
    c = Company(tenant_id=tenant.id, name=name, is_active=active, created_at=now, updated_at=now)
    db.add(c)
    await db.commit()
    return c


class TestDepartmentApi:
    async def test_anlegen_ohne_firma(self, db, client, tenant):
        """Kein Firmenzwang mehr (frueher 422)."""
        hdr = await _headers(db, tenant.id)
        r = await client.post("/api/v1/departments", json={"name": "Lager"}, headers=hdr)
        assert r.status_code == 201, r.text
        alle = (await client.get("/api/v1/departments", headers=hdr)).json()
        d = next(x for x in alle if x["name"] == "Lager")
        assert d["company_id"] is None

    async def test_company_id_im_body_wird_ignoriert(self, db, client, tenant):
        a = await _company(db, tenant, "Alpha")
        hdr = await _headers(db, tenant.id)
        r = await client.post(
            "/api/v1/departments", json={"name": "Fuhrpark", "company_id": str(a.id)}, headers=hdr
        )
        assert r.status_code == 201, r.text
        alle = (await client.get("/api/v1/departments", headers=hdr)).json()
        assert next(x for x in alle if x["name"] == "Fuhrpark")["company_id"] is None

    async def test_umbenennen_ohne_firma_kein_422(self, db, client, tenant):
        """Frueher scheiterte PATCH ohne Firma mit 'Firma erforderlich'."""
        hdr = await _headers(db, tenant.id)
        dep_id = (
            await client.post("/api/v1/departments", json={"name": "Lager"}, headers=hdr)
        ).json()["id"]
        r = await client.patch(
            f"/api/v1/departments/{dep_id}", json={"name": "Lager Nord"}, headers=hdr
        )
        assert r.status_code == 200, r.text
        alle = (await client.get("/api/v1/departments", headers=hdr)).json()
        assert next(x for x in alle if x["id"] == dep_id)["name"] == "Lager Nord"

    async def test_firmenfilter_liefert_weiterhin_alle(self, db, client, tenant):
        """company_id-Parameter ist deprecated — er darf nichts mehr wegfiltern."""
        a = await _company(db, tenant, "Alpha")
        hdr = await _headers(db, tenant.id)
        await client.post("/api/v1/departments", json={"name": "Lager"}, headers=hdr)
        gefiltert = (await client.get(f"/api/v1/departments?company_id={a.id}", headers=hdr)).json()
        assert [d["name"] for d in gefiltert] == ["Lager"]


class TestUserDepartmentKonsistenz:
    async def test_gleicher_bereich_fuer_beide_firmen(self, db, client, tenant, employee):
        """Ein Bereich passt zu Mitarbeitern beider Firmen (frueher 422)."""
        a = await _company(db, tenant, "Alpha")
        b = await _company(db, tenant, "Beta")
        hdr = await _headers(db, tenant.id)
        dep_id = (
            await client.post("/api/v1/departments", json={"name": "Logistik"}, headers=hdr)
        ).json()["id"]

        r1 = await client.patch(
            f"/api/v1/users/{employee.id}",
            json={"company_id": str(a.id), "department_id": dep_id},
            headers=hdr,
        )
        assert r1.status_code == 200, r1.text
        r2 = await client.patch(
            f"/api/v1/users/{employee.id}",
            json={"company_id": str(b.id), "department_id": dep_id},
            headers=hdr,
        )
        assert r2.status_code == 200, r2.text

    async def test_unbekannter_bereich_weiter_abgelehnt(self, db, client, tenant, employee):
        """Tenant-Pruefung bleibt (#100)."""
        import uuid as _uuid

        r = await client.patch(
            f"/api/v1/users/{employee.id}",
            json={"department_id": str(_uuid.uuid4())},
            headers=await _headers(db, tenant.id),
        )
        assert r.status_code == 422, r.text
