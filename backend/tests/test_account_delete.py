"""DELETE /accounts/{id}: harte Loeschung mit Schutzregeln (Issue #106)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from zeiterfassung.core.security import create_access_token, hash_password
from zeiterfassung.models.account import Account


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


async def _account(db, tenant, email, *, active=True, superadmin=False, employee_id=None):
    now = datetime.now(tz=UTC)
    acc = Account(
        tenant_id=tenant.id,
        email=email,
        hashed_password=hash_password("Passw0rd!"),
        is_active=active,
        is_superadmin=superadmin,
        employee_id=employee_id,
        created_at=now,
        updated_at=now,
    )
    db.add(acc)
    await db.commit()
    return acc


def _headers(tenant_id, account_id, *, superadmin=True, perms=("admin:accounts",)):
    tok = create_access_token(
        user_id=str(account_id),
        tenant_id=str(tenant_id),
        permissions=list(perms),
        department_id=None,
        role_type="geschaeftsfuehrung",
        data_scope="global",
        is_superadmin=superadmin,
    )
    return {"Authorization": f"Bearer {tok}"}


class TestAccountDelete:
    async def test_deaktiviertes_konto_loeschen(self, db, client, tenant):
        me = await _account(db, tenant, "me@t.de", superadmin=True)
        ziel = await _account(db, tenant, "alt@t.de", active=False)
        r = await client.delete(f"/api/v1/accounts/{ziel.id}", headers=_headers(tenant.id, me.id))
        assert r.status_code == 204, r.text
        liste = (await client.get("/api/v1/accounts", headers=_headers(tenant.id, me.id))).json()
        assert not any(a["email"] == "alt@t.de" for a in liste)

    async def test_eigenes_konto_409(self, db, client, tenant):
        me = await _account(db, tenant, "me@t.de", superadmin=True)
        r = await client.delete(f"/api/v1/accounts/{me.id}", headers=_headers(tenant.id, me.id))
        assert r.status_code == 409, r.text

    async def test_letzter_superadmin_409(self, db, client, tenant):
        # Genau EIN aktiver Superadmin (sa). Geloescht wird er von einem anderen
        # Konto (actor mit admin:accounts) -> muss blocken (letzter aktiver SA).
        sa = await _account(db, tenant, "sa@t.de", superadmin=True)
        actor = await _account(db, tenant, "actor@t.de", superadmin=True)
        # actor ist zwar auch SA -> damit sa NICHT der letzte waere. Deshalb
        # actor VOR dem Loeschen deaktivieren, sa bleibt einziger aktiver SA.
        actor.is_active = False
        await db.commit()
        # Ein drittes Konto mit dem Recht loescht -> es MUSS blocken, weil sa
        # der letzte AKTIVE Superadmin ist.
        deleter = await _account(db, tenant, "del@t.de", superadmin=False)
        r = await client.delete(
            f"/api/v1/accounts/{sa.id}",
            headers=_headers(tenant.id, deleter.id, superadmin=False),
        )
        assert r.status_code == 409, r.text

    async def test_superadmin_loeschbar_wenn_zweiter_existiert(self, db, client, tenant):
        me = await _account(db, tenant, "me@t.de", superadmin=True)
        ziel = await _account(db, tenant, "sa2@t.de", superadmin=True)
        r = await client.delete(f"/api/v1/accounts/{ziel.id}", headers=_headers(tenant.id, me.id))
        assert r.status_code == 204, r.text

    async def test_ohne_recht_403(self, db, client, tenant):
        me = await _account(db, tenant, "me@t.de", superadmin=True)
        ziel = await _account(db, tenant, "alt@t.de", active=False)
        r = await client.delete(
            f"/api/v1/accounts/{ziel.id}",
            headers=_headers(tenant.id, me.id, superadmin=False, perms=("timesheet:view_own",)),
        )
        assert r.status_code == 403, r.text

    async def test_verknuepftes_konto_loeschbar_user_bleibt(self, db, client, tenant, employee):
        me = await _account(db, tenant, "me@t.de", superadmin=True)
        ziel = await _account(db, tenant, "emp@t.de", employee_id=employee.id)
        r = await client.delete(f"/api/v1/accounts/{ziel.id}", headers=_headers(tenant.id, me.id))
        assert r.status_code == 204, r.text
        from sqlalchemy import select

        from zeiterfassung.models.user import User

        u = (await db.execute(select(User).where(User.id == employee.id))).scalar_one_or_none()
        assert u is not None  # Mitarbeiter bleibt (C-locker)

    async def test_fremder_tenant_404(self, db, client, tenant):
        me = await _account(db, tenant, "me@t.de", superadmin=True)
        r = await client.delete(
            f"/api/v1/accounts/{uuid.uuid4()}", headers=_headers(tenant.id, me.id)
        )
        assert r.status_code == 404, r.text
