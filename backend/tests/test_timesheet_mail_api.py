"""POST /admin/timesheet-mail: die Speichern-Antwort darf das SMTP-Passwort
nicht enthalten — nur ``password_set`` wie im GET. (Vorher kam das gespeicherte
Passwort im Klartext in jeder POST-Antwort zurueck.)"""

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


async def _headers(db: AsyncSession, tenant_id: uuid.UUID) -> dict[str, str]:
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


class TestPostMaskiertPasswort:
    async def test_post_antwort_enthaelt_kein_passwort(self, db, client, tenant) -> None:
        await db.commit()
        headers = await _headers(db, tenant.id)
        r = await client.post(
            "/api/v1/admin/timesheet-mail",
            json={
                "transport": "smtp",
                "smtp": {"host": "mail.test.de", "port": 587, "user": "u", "password": "geheim"},
            },
            headers=headers,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["smtp"]["password_set"] is True
        assert "password" not in body["smtp"]
        assert "geheim" not in r.text

    async def test_leeres_passwort_loescht_nicht_und_leakt_nicht(self, db, client, tenant) -> None:
        await db.commit()
        headers = await _headers(db, tenant.id)
        await client.post(
            "/api/v1/admin/timesheet-mail",
            json={"transport": "smtp", "smtp": {"host": "mail.test.de", "password": "geheim"}},
            headers=headers,
        )
        # Zweiter POST ohne Passwort: gespeichertes bleibt, Antwort bleibt sauber.
        r = await client.post(
            "/api/v1/admin/timesheet-mail",
            json={"smtp": {"host": "mail.test.de", "user": "neu"}},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["smtp"]["password_set"] is True
        assert "geheim" not in r.text

    async def test_get_bleibt_maskiert(self, db, client, tenant) -> None:
        await db.commit()
        headers = await _headers(db, tenant.id)
        await client.post(
            "/api/v1/admin/timesheet-mail",
            json={"transport": "smtp", "smtp": {"host": "h", "password": "geheim"}},
            headers=headers,
        )
        r = await client.get("/api/v1/admin/timesheet-mail", headers=headers)
        assert r.status_code == 200
        assert "geheim" not in r.text
        assert r.json()["smtp"]["password_set"] is True
