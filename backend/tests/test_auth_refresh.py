"""Tests for POST /api/v1/auth/refresh (Bead zeiterfassung-fkw.1, PR A / Phase 1;
updated for fkw.4/PR D — cookie-only, no more refresh_token in the JSON body).

Covers the two bugs fixed here:
1. Stateless refresh (no DB check on account existence/is_active) — a refresh
   token used to keep working after an account was deactivated.
2. Claim loss (new access token had no permissions/role_type/data_scope/...) —
   a refresh effectively made a user rightless until they logged in again.

Both ``authenticate()`` (login) and ``refresh_tokens()`` now share
``_issue_token_pair()`` so login and refresh can never diverge.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from zeiterfassung.core.security import (
    create_access_token,
    create_refresh_token,
    decode_access_token,
)
from zeiterfassung.models.tenant import Tenant
from zeiterfassung.models.user import User


@pytest_asyncio.fixture
async def app_with_db(engine):
    """FastAPI app instance with the test DB engine injected."""
    from sqlalchemy.ext.asyncio import AsyncSession as _AS
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from zeiterfassung.api import deps
    from zeiterfassung.main import create_app

    test_factory = async_sessionmaker(engine, expire_on_commit=False, class_=_AS)

    async def override_get_db():
        async with test_factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    app = create_app()
    app.dependency_overrides[deps.get_db] = override_get_db
    yield app
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client(app_with_db):
    # https:// so the client's cookie jar actually stores the Secure refresh
    # cookie the server sets (default ZEITERFASSUNG_AUTH_COOKIE_SECURE=true).
    async with AsyncClient(transport=ASGITransport(app=app_with_db), base_url="https://test") as c:
        yield c


REFRESH_COOKIE_NAME = "zeit_refresh"


def _refresh_cookie_value(resp) -> str | None:
    return resp.cookies.get(REFRESH_COOKIE_NAME)


@pytest_asyncio.fixture
async def tenant(db: AsyncSession) -> Tenant:
    now = datetime.now(tz=UTC)
    t = Tenant(
        name="Refresh Test GmbH",
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


@pytest_asyncio.fixture
async def employee_user(db: AsyncSession, tenant: Tenant) -> User:
    from zeiterfassung.core.security import hash_password

    now = datetime.now(tz=UTC)
    u = User(
        tenant_id=tenant.id,
        email="klaus.mueller@test.de",
        hashed_password=hash_password("Passw0rd!"),
        first_name="Klaus",
        last_name="Müller",
        employee_number="EMP001",
        contracted_hours_per_week=40.0,
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    db.add(u)
    await db.flush()
    return u


@pytest_asyncio.fixture
async def account(db: AsyncSession, tenant: Tenant, employee_user: User):
    """A login Account with a real role (permissions/data_scope/role_type)."""
    from zeiterfassung.core.security import hash_password
    from zeiterfassung.models.account import Account, AccountRole
    from zeiterfassung.models.user import Role

    now = datetime.now(tz=UTC)
    role = Role(
        tenant_id=tenant.id,
        name="Teamleiter",
        permissions=["timesheet:view_team", "leave:approve_team"],
        data_scope="department",
        role_type="teamleiter",
        created_at=now,
        updated_at=now,
    )
    db.add(role)
    await db.flush()

    acc = Account(
        tenant_id=tenant.id,
        email="klaus.mueller@test.de",
        hashed_password=hash_password("Passw0rd!"),
        is_superadmin=False,
        employee_id=employee_user.id,
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    db.add(acc)
    await db.flush()

    db.add(AccountRole(account_id=acc.id, role_id=role.id))
    await db.flush()
    await db.commit()
    return acc


def _refresh_url() -> str:
    return "/api/v1/auth/refresh"


class TestAuthRefreshClaims:
    """Refresh must rebuild the exact same claims as a fresh login."""

    async def test_refresh_returns_login_equivalent_claims(
        self, client: AsyncClient, tenant: Tenant, account
    ) -> None:
        login_r = await client.post(
            f"/api/v1/auth/login/{tenant.id}",
            json={"email": "klaus.mueller@test.de", "password": "Passw0rd!"},
        )
        assert login_r.status_code == 200, login_r.text
        login_body = login_r.json()
        login_claims = decode_access_token(login_body["access_token"])

        # Cookie (persisted on the shared httpx client) drives the refresh —
        # no body (fkw.4, body fallback removed).
        refresh_r = await client.post(_refresh_url())
        assert refresh_r.status_code == 200, refresh_r.text
        refresh_body = refresh_r.json()
        refresh_claims = decode_access_token(refresh_body["access_token"])

        for claim in (
            "permissions",
            "data_scope",
            "role_type",
            "is_superadmin",
            "tenant_id",
            "department_id",
            "department_ids",
            "employee_id",
        ):
            assert refresh_claims.get(claim) == login_claims.get(claim), claim
        # sub (account id) must stay identical across refresh.
        assert refresh_claims["sub"] == login_claims["sub"]

    async def test_refresh_response_shape_unchanged(
        self, client: AsyncClient, tenant: Tenant, account
    ) -> None:
        """Public response shape: access_token/token_type/expires_in only —
        the refresh token travels exclusively via the httpOnly cookie (fkw.4)."""
        login_r = await client.post(
            f"/api/v1/auth/login/{tenant.id}",
            json={"email": "klaus.mueller@test.de", "password": "Passw0rd!"},
        )
        assert login_r.status_code == 200, login_r.text

        refresh_r = await client.post(_refresh_url())
        assert refresh_r.status_code == 200, refresh_r.text
        body = refresh_r.json()
        assert set(body.keys()) >= {"access_token", "token_type", "expires_in"}
        assert "refresh_token" not in body
        assert isinstance(body["access_token"], str) and body["access_token"]
        assert body["expires_in"] > 0
        assert _refresh_cookie_value(refresh_r)


class TestAuthRefreshAccountState:
    """Refresh must be revoked as soon as the underlying account is not usable."""

    async def test_refresh_after_deactivation_is_401(
        self, client: AsyncClient, tenant: Tenant, account, db: AsyncSession
    ) -> None:
        login_r = await client.post(
            f"/api/v1/auth/login/{tenant.id}",
            json={"email": "klaus.mueller@test.de", "password": "Passw0rd!"},
        )
        assert login_r.status_code == 200, login_r.text

        account.is_active = False
        db.add(account)
        await db.commit()

        # Cookie (from login, still on the shared client) drives the refresh.
        refresh_r = await client.post(_refresh_url())
        assert refresh_r.status_code == 401

    async def test_refresh_with_nonexistent_account_is_401(
        self, client: AsyncClient, tenant: Tenant
    ) -> None:
        bogus_refresh = create_refresh_token(
            user_id=str(uuid.uuid4()),
            tenant_id=str(tenant.id),
        )
        client.cookies.set(REFRESH_COOKIE_NAME, bogus_refresh)
        r = await client.post(_refresh_url())
        assert r.status_code == 401

    async def test_refresh_with_access_token_is_401(
        self, client: AsyncClient, tenant: Tenant, account
    ) -> None:
        access_token = create_access_token(user_id=str(account.id), tenant_id=str(tenant.id))
        client.cookies.set(REFRESH_COOKIE_NAME, access_token)
        r = await client.post(_refresh_url())
        assert r.status_code == 401
