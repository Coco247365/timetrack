"""7tu: Urlaubsanspruch-Default war inkonsistent (Schema 30 vs. Service-Fallback
24) — jetzt EIN mandantenweiter, konfigurierbarer Default fuer beide Stellen.
"""

from __future__ import annotations

import uuid

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from zeiterfassung.core.security import create_access_token
from zeiterfassung.schemas.user import UserCreate
from zeiterfassung.services.absence_service import (
    DEFAULT_VACATION_DAYS_PER_YEAR,
    resolve_default_vacation_entitlement_days,
)
from zeiterfassung.services.user_service import create_user


class TestResolveDefault:
    def test_no_tenant_setting_uses_fallback(self, tenant) -> None:
        assert resolve_default_vacation_entitlement_days(tenant) == DEFAULT_VACATION_DAYS_PER_YEAR

    def test_tenant_setting_overrides_fallback(self, tenant) -> None:
        tenant.settings = {"default_vacation_entitlement_days": 27.5}
        assert resolve_default_vacation_entitlement_days(tenant) == 27.5

    def test_none_tenant_uses_fallback(self) -> None:
        assert resolve_default_vacation_entitlement_days(None) == DEFAULT_VACATION_DAYS_PER_YEAR


class TestCreateUserUsesTenantDefault:
    async def test_unset_uses_tenant_configured_default(self, db, tenant) -> None:
        tenant.settings = {"default_vacation_entitlement_days": 26.0}
        await db.flush()
        user = await create_user(
            db,
            tenant.id,
            UserCreate(email="new1@test.de", first_name="Neu", last_name="Eins"),
        )
        assert user.vacation_entitlement_days == 26.0

    async def test_unset_without_tenant_setting_uses_fallback(self, db, tenant) -> None:
        user = await create_user(
            db,
            tenant.id,
            UserCreate(email="new2@test.de", first_name="Neu", last_name="Zwei"),
        )
        assert user.vacation_entitlement_days == DEFAULT_VACATION_DAYS_PER_YEAR

    async def test_explicit_value_wins_over_tenant_default(self, db, tenant) -> None:
        tenant.settings = {"default_vacation_entitlement_days": 26.0}
        await db.flush()
        user = await create_user(
            db,
            tenant.id,
            UserCreate(
                email="new3@test.de",
                first_name="Neu",
                last_name="Drei",
                vacation_entitlement_days=30.0,
            ),
        )
        assert user.vacation_entitlement_days == 30.0


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


class TestVacationSettingsApi:
    async def test_get_default_before_configured(self, db, client, tenant) -> None:
        await db.commit()
        r = await client.get(
            "/api/v1/admin/vacation-settings",
            headers=await _headers(db, tenant.id, ["admin:settings"]),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["default_vacation_entitlement_days"] == DEFAULT_VACATION_DAYS_PER_YEAR
        assert body["is_custom"] is False

    async def test_patch_sets_tenant_default(self, db, client, tenant) -> None:
        await db.commit()
        headers = await _headers(db, tenant.id, ["admin:settings"])
        r = await client.patch(
            "/api/v1/admin/vacation-settings",
            json={"default_vacation_entitlement_days": 28.0},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["default_vacation_entitlement_days"] == 28.0
        assert body["is_custom"] is True

        r = await client.get("/api/v1/admin/vacation-settings", headers=headers)
        assert r.json()["default_vacation_entitlement_days"] == 28.0

    async def test_patch_rejects_out_of_range(self, db, client, tenant) -> None:
        await db.commit()
        r = await client.patch(
            "/api/v1/admin/vacation-settings",
            json={"default_vacation_entitlement_days": 400.0},
            headers=await _headers(db, tenant.id, ["admin:settings"]),
        )
        assert r.status_code == 422, r.text

    async def test_requires_admin_settings_permission(self, db, client, tenant) -> None:
        await db.commit()
        r = await client.get(
            "/api/v1/admin/vacation-settings",
            headers=await _headers(db, tenant.id, ["timesheet:view_own"]),
        )
        assert r.status_code == 403, r.text
