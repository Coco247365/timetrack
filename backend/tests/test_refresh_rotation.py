"""Tests for Refresh-Token-Rotation + Revocation + httpOnly-Cookie
(Bead zeiterfassung-fkw.2/fkw.4, PR B / Phase 2+3, updated for PR D / Phase 6).

Covers:
1. Login sets the httpOnly ``zeit_refresh`` cookie and a ``refresh_sessions`` row.
   The JSON body does NOT carry the refresh token (fkw.4 — cookie-only).
2. Refresh via cookie rotates: old session revoked (``rotated_to_jti``), new
   session same ``family_id``.
3. The refresh-token body fallback is REMOVED (fkw.4/Phase 6): no cookie ->
   401, even with a (still valid) token in the JSON body.
4. Reuse of an already-rotated refresh token revokes the WHOLE family and is
   audit-logged (``auth.refresh_reuse_detected``).
5. Logout revokes the family and deletes the cookie — no access token
   required (fkw.4).
6. A pre-fkw.2 token (no ``jti`` claim, no session row) is rejected.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from zeiterfassung.core.security import create_refresh_token
from zeiterfassung.models.audit import AuditLog
from zeiterfassung.models.refresh_session import RefreshSession
from zeiterfassung.models.tenant import Tenant
from zeiterfassung.models.user import User

REFRESH_COOKIE_NAME = "zeit_refresh"
REFRESH_COOKIE_PATH = "/api/v1/auth"


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
    async with AsyncClient(transport=ASGITransport(app=app_with_db), base_url="https://test") as c:
        yield c


@pytest_asyncio.fixture
async def tenant(db: AsyncSession) -> Tenant:
    now = datetime.now(tz=UTC)
    t = Tenant(
        name="Rotation Test GmbH",
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
        email="rotation.user@test.de",
        hashed_password=hash_password("Passw0rd!"),
        first_name="Rota",
        last_name="Tion",
        employee_number="EMP002",
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
        email="rotation.user@test.de",
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


def _login_url(tenant_id: uuid.UUID) -> str:
    return f"/api/v1/auth/login/{tenant_id}"


async def _login(client: AsyncClient, tenant_id: uuid.UUID):
    return await client.post(
        _login_url(tenant_id),
        json={"email": "rotation.user@test.de", "password": "Passw0rd!"},
    )


def _cookie_headers(resp) -> list[str]:
    return resp.headers.get_list("set-cookie")


def _refresh_cookie_value(resp) -> str | None:
    """Extract the raw refresh-token value httpx parsed from Set-Cookie."""
    return resp.cookies.get(REFRESH_COOKIE_NAME)


class TestLoginSetsCookie:
    async def test_login_sets_httponly_refresh_cookie(
        self, client: AsyncClient, tenant: Tenant, account, db: AsyncSession
    ) -> None:
        resp = await _login(client, tenant.id)
        assert resp.status_code == 200, resp.text

        cookies = _cookie_headers(resp)
        assert len(cookies) == 1
        cookie = cookies[0]
        assert cookie.startswith(f"{REFRESH_COOKIE_NAME}=")
        assert "httponly" in cookie.lower()
        assert "samesite=strict" in cookie.lower()
        assert f"path={REFRESH_COOKIE_PATH}".lower() in cookie.lower()

        # Body NEVER carries refresh_token (fkw.4 — cookie is the only channel).
        body = resp.json()
        assert "refresh_token" not in body
        assert body["access_token"]

        rows = (
            (
                await db.execute(
                    select(RefreshSession).where(RefreshSession.account_id == account.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].revoked_at is None
        assert rows[0].jti


class TestRefreshRotation:
    async def test_refresh_via_cookie_rotates_session(
        self, client: AsyncClient, tenant: Tenant, account, db: AsyncSession
    ) -> None:
        login_r = await _login(client, tenant.id)
        assert login_r.status_code == 200, login_r.text
        old_refresh_token = _refresh_cookie_value(login_r)
        assert old_refresh_token

        # No body — cookie (persisted on the shared httpx client) drives the refresh.
        refresh_r = await client.post("/api/v1/auth/refresh")
        assert refresh_r.status_code == 200, refresh_r.text
        refresh_body = refresh_r.json()
        assert refresh_body["access_token"]
        assert "refresh_token" not in refresh_body

        # Rotated cookie present, and it carries a different token value.
        cookies = _cookie_headers(refresh_r)
        assert len(cookies) == 1
        assert cookies[0].startswith(f"{REFRESH_COOKIE_NAME}=")
        new_refresh_token = _refresh_cookie_value(refresh_r)
        assert new_refresh_token
        assert new_refresh_token != old_refresh_token

        rows = (
            (
                await db.execute(
                    select(RefreshSession)
                    .where(RefreshSession.account_id == account.id)
                    .order_by(RefreshSession.created_at)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 2
        old_row, new_row = rows
        assert old_row.revoked_at is not None
        assert old_row.rotated_to_jti == new_row.jti
        assert new_row.revoked_at is None
        assert new_row.family_id == old_row.family_id

    async def test_refresh_without_cookie_is_401(
        self, app_with_db, tenant: Tenant, account
    ) -> None:
        """Body-Fallback ist entfernt (fkw.4/Phase 6) — cookie-only."""
        async with AsyncClient(
            transport=ASGITransport(app=app_with_db), base_url="https://test"
        ) as bare_client:
            no_cookie_no_body = await bare_client.post("/api/v1/auth/refresh")
        assert no_cookie_no_body.status_code == 401, no_cookie_no_body.text

    async def test_refresh_body_fallback_removed(
        self, app_with_db, tenant: Tenant, account
    ) -> None:
        """A STILL-VALID refresh token in the JSON body is ignored — proves
        removal, not merely that an empty request 401s."""
        async with AsyncClient(
            transport=ASGITransport(app=app_with_db), base_url="https://test"
        ) as login_client:
            login_r = await _login(login_client, tenant.id)
        assert login_r.status_code == 200, login_r.text
        refresh_token = _refresh_cookie_value(login_r)
        assert refresh_token

        async with AsyncClient(
            transport=ASGITransport(app=app_with_db), base_url="https://test"
        ) as bare_client:
            refresh_r = await bare_client.post(
                "/api/v1/auth/refresh", json={"refresh_token": refresh_token}
            )
        assert refresh_r.status_code == 401, refresh_r.text


class TestReuseDetection:
    async def test_reusing_rotated_token_revokes_whole_family(
        self, client: AsyncClient, tenant: Tenant, account, db: AsyncSession
    ) -> None:
        login_r = await _login(client, tenant.id)
        old_refresh_token = _refresh_cookie_value(login_r)
        assert old_refresh_token

        first_refresh = await client.post("/api/v1/auth/refresh")
        assert first_refresh.status_code == 200, first_refresh.text
        newest_refresh_token = _refresh_cookie_value(first_refresh)
        assert newest_refresh_token

        # Replay the OLD (already rotated) refresh token by forcing it back
        # into the client's cookie jar (which currently holds the rotated,
        # live token). This mirrors an attacker who only has the leaked old
        # cookie value, not the legitimate browser's live session.
        client.cookies.set(REFRESH_COOKIE_NAME, old_refresh_token)
        reuse_r = await client.post("/api/v1/auth/refresh")
        assert reuse_r.status_code == 401, reuse_r.text

        # The whole family is now dead — even the newest token is rejected.
        client.cookies.set(REFRESH_COOKIE_NAME, newest_refresh_token)
        dead_r = await client.post("/api/v1/auth/refresh")
        assert dead_r.status_code == 401, dead_r.text

        audit_rows = (
            (
                await db.execute(
                    select(AuditLog).where(AuditLog.action == "auth.refresh_reuse_detected")
                )
            )
            .scalars()
            .all()
        )
        assert len(audit_rows) >= 1
        assert audit_rows[0].tenant_id == tenant.id

        rows = (
            (
                await db.execute(
                    select(RefreshSession).where(RefreshSession.account_id == account.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 2
        assert all(r.revoked_at is not None for r in rows)


class TestLogout:
    async def test_logout_revokes_family_and_deletes_cookie(
        self, client: AsyncClient, tenant: Tenant, account, db: AsyncSession
    ) -> None:
        login_r = await _login(client, tenant.id)
        assert login_r.status_code == 200, login_r.text

        # No Authorization header — the refresh cookie (on the shared client)
        # IS the credential (fkw.4, no access token required for logout).
        logout_r = await client.post("/api/v1/auth/logout")
        assert logout_r.status_code == 204, logout_r.text

        cookies = _cookie_headers(logout_r)
        assert len(cookies) == 1
        # A deleted cookie is expressed via Max-Age=0 / an expiry in the past.
        assert "max-age=0" in cookies[0].lower() or "01 jan 1970" in cookies[0].lower()

        rows = (
            (
                await db.execute(
                    select(RefreshSession).where(RefreshSession.account_id == account.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].revoked_at is not None

        refresh_r = await client.post("/api/v1/auth/refresh")
        assert refresh_r.status_code == 401, refresh_r.text

    async def test_logout_without_any_credentials_is_still_204(
        self, app_with_db, tenant: Tenant, account
    ) -> None:
        """Best-effort: no cookie, no access token -> still 204 (fkw.4)."""
        async with AsyncClient(
            transport=ASGITransport(app=app_with_db), base_url="https://test"
        ) as bare_client:
            logout_r = await bare_client.post("/api/v1/auth/logout")
        assert logout_r.status_code == 204, logout_r.text


class TestPrePR_BToken:
    async def test_refresh_with_token_missing_jti_is_401(
        self, client: AsyncClient, tenant: Tenant, account
    ) -> None:
        legacy_token = create_refresh_token(user_id=str(account.id), tenant_id=str(tenant.id))
        client.cookies.set(REFRESH_COOKIE_NAME, legacy_token)
        r = await client.post("/api/v1/auth/refresh")
        assert r.status_code == 401
