"""Cross-Tenant-Regressiontests fuer den Mandanten-Scoping-Guard (#122).

Funktionaler Nachweis (keine Source-Greps): zwei Mandanten mit eigenen Usern
werden angelegt; jede konvertierte Lesepfad-Funktion darf fuer Mandant A
NIEMALS Zeilen von Mandant B zurueckgeben — und umgekehrt. Deckt die in Bead
115k konvertierte erste Tranche ab:

- ``user_service.get_user_by_id``
- ``user_service.get_user_by_email``
- ``user_service.list_users``
- ``GET /api/v1/admin/employees`` (nutzt denselben ``tenant_scoped``-Helfer)
"""

from __future__ import annotations

import uuid as uuid_module
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from zeiterfassung.core.security import hash_password
from zeiterfassung.models.tenant import Tenant
from zeiterfassung.models.user import User
from zeiterfassung.services.user_service import (
    UserNotFoundError,
    get_user_by_email,
    get_user_by_id,
    list_users,
)


async def _make_tenant(db: AsyncSession, name: str) -> Tenant:
    now = datetime.now(tz=UTC)
    t = Tenant(
        name=name,
        timezone="Europe/Berlin",
        locale="de-DE",
        bundesland="HH",
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    db.add(t)
    await db.flush()
    return t


async def _make_user(
    db: AsyncSession, tenant: Tenant, *, email: str, first: str, last: str
) -> User:
    now = datetime.now(tz=UTC)
    u = User(
        tenant_id=tenant.id,
        email=email,
        hashed_password=hash_password("Passw0rd!"),
        first_name=first,
        last_name=last,
        employee_number=f"EMP-{uuid_module.uuid4().hex[:8]}",
        contracted_hours_per_week=40.0,
        vacation_entitlement_days=24.0,
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    db.add(u)
    await db.flush()
    return u


@pytest_asyncio.fixture
async def two_tenants(db: AsyncSession):
    """Zwei Mandanten mit je einem User — Fixture-Namen bewusst neutral (A/B)."""
    tenant_a = await _make_tenant(db, "Mandant A GmbH")
    tenant_b = await _make_tenant(db, "Mandant B GmbH")
    user_a = await _make_user(db, tenant_a, email="alice@a.example", first="Alice", last="A")
    user_b = await _make_user(db, tenant_b, email="bob@b.example", first="Bob", last="B")
    await db.commit()
    return tenant_a, tenant_b, user_a, user_b


class TestUserServiceCrossTenant:
    async def test_get_user_by_id_verweigert_fremden_mandanten(self, db, two_tenants):
        tenant_a, tenant_b, user_a, user_b = two_tenants

        # Eigener Mandant funktioniert normal.
        got = await get_user_by_id(db, tenant_a.id, user_a.id)
        assert got.id == user_a.id

        # Users ID von Mandant B unter Mandant A abfragen -> nicht gefunden,
        # NICHT die fremde Zeile.
        with pytest.raises(UserNotFoundError):
            await get_user_by_id(db, tenant_a.id, user_b.id)

        # Und umgekehrt.
        with pytest.raises(UserNotFoundError):
            await get_user_by_id(db, tenant_b.id, user_a.id)

    async def test_get_user_by_email_verweigert_fremden_mandanten(self, db, two_tenants):
        tenant_a, tenant_b, user_a, user_b = two_tenants

        assert (await get_user_by_email(db, tenant_a.id, user_a.email)) is not None
        # Bob existiert, aber nicht unter Mandant A.
        assert (await get_user_by_email(db, tenant_a.id, user_b.email)) is None
        assert (await get_user_by_email(db, tenant_b.id, user_a.email)) is None

    async def test_list_users_liefert_nur_eigenen_mandanten(self, db, two_tenants):
        tenant_a, tenant_b, user_a, user_b = two_tenants

        users_a = await list_users(db, tenant_a.id, active_only=False)
        users_b = await list_users(db, tenant_b.id, active_only=False)

        ids_a = {u.id for u in users_a}
        ids_b = {u.id for u in users_b}

        assert user_a.id in ids_a
        assert user_b.id not in ids_a
        assert user_b.id in ids_b
        assert user_a.id not in ids_b
        # Keine Ueberschneidung der Ergebnismengen ueberhaupt.
        assert ids_a.isdisjoint(ids_b)


class TestAdminEmployeesCrossTenant:
    async def test_admin_employees_liefert_nur_eigenen_mandanten(self, db, two_tenants):
        tenant_a, tenant_b, user_a, user_b = two_tenants

        emps_a = await self._fetch_admin_employees(db, tenant_a)
        emps_b = await self._fetch_admin_employees(db, tenant_b)

        ids_a = {e["id"] for e in emps_a}
        ids_b = {e["id"] for e in emps_b}

        assert str(user_a.id) in ids_a
        assert str(user_b.id) not in ids_a
        assert str(user_b.id) in ids_b
        assert str(user_a.id) not in ids_b
        assert ids_a.isdisjoint(ids_b)

    async def _fetch_admin_employees(self, db: AsyncSession, tenant: Tenant) -> list[dict]:
        from httpx import ASGITransport, AsyncClient

        from tests.conftest import ensure_login_account
        from zeiterfassung.api import deps
        from zeiterfassung.core.security import create_access_token
        from zeiterfassung.main import create_app

        factory = async_sessionmaker(db.bind, expire_on_commit=False, class_=AsyncSession)

        async def override_get_db():
            async with factory() as session:
                yield session

        app = create_app()
        app.dependency_overrides[deps.get_db] = override_get_db
        acc = await ensure_login_account(db, tenant.id)
        token = create_access_token(
            user_id=str(acc.id),
            tenant_id=str(tenant.id),
            permissions=["users:read_all"],
        )
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="https://test"
            ) as client:
                resp = await client.get(
                    "/api/v1/admin/employees", headers={"Authorization": f"Bearer {token}"}
                )
        finally:
            app.dependency_overrides.clear()
        assert resp.status_code == 200
        return resp.json()
