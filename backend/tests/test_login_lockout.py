"""Tests for DB-based login lockout (Bead zeiterfassung-fkw.4, PR D / Phase 6).

User-Entscheid 2026-07-17: 10 Fehlversuche in Folge -> 5 min Sperre, kein Redis.

Covers:
1. 10 wrong attempts, then an 11th attempt with the CORRECT password is still
   401 (locked) — with the same generic message as a wrong password (no
   lockout oracle).
2. After the lock expires, a correct login succeeds and resets the counter.
3. A successful login (below the threshold) resets ``failed_login_count``.
4. Locking writes an ``account.locked`` audit-log entry.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from zeiterfassung.api.v1.users import GENERIC_AUTH_ERROR
from zeiterfassung.models.audit import AuditLog
from zeiterfassung.models.tenant import Tenant
from zeiterfassung.models.user import User
from zeiterfassung.services.user_service import LOGIN_LOCKOUT_THRESHOLD

EMAIL = "lockout.user@test.de"
CORRECT_PASSWORD = "Passw0rd!"
WRONG_PASSWORD = "totally-wrong"


@pytest.fixture(autouse=True)
def _relax_auth_rate_limits(monkeypatch):
    from zeiterfassung.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "auth_rate_limit_login_ip_max", 10_000)
    monkeypatch.setattr(s, "auth_rate_limit_login_email_max", 10_000)


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
        name="Lockout Test GmbH",
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
        email=EMAIL,
        hashed_password=hash_password(CORRECT_PASSWORD),
        first_name="Lock",
        last_name="Out",
        employee_number="EMP003",
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
        name="Mitarbeiter",
        permissions=["timesheet:view_own"],
        data_scope="self",
        role_type="mitarbeiter",
        created_at=now,
        updated_at=now,
    )
    db.add(role)
    await db.flush()

    acc = Account(
        tenant_id=tenant.id,
        email=EMAIL,
        hashed_password=hash_password(CORRECT_PASSWORD),
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


def _login_url(tenant_id) -> str:
    return f"/api/v1/auth/login/{tenant_id}"


async def _attempt(client: AsyncClient, tenant_id, password: str):
    return await client.post(_login_url(tenant_id), json={"email": EMAIL, "password": password})


class TestLockoutAfterThreshold:
    async def test_ten_wrong_attempts_then_correct_password_still_401(
        self, client: AsyncClient, tenant: Tenant, account
    ) -> None:
        for _ in range(LOGIN_LOCKOUT_THRESHOLD):
            r = await _attempt(client, tenant.id, WRONG_PASSWORD)
            assert r.status_code == 401, r.text
            assert r.json()["detail"] == GENERIC_AUTH_ERROR

        # 11th attempt uses the CORRECT password — still 401 (locked), same
        # generic message (no lockout oracle: a caller can't distinguish
        # "wrong password" from "account locked").
        r = await _attempt(client, tenant.id, CORRECT_PASSWORD)
        assert r.status_code == 401, r.text
        assert r.json()["detail"] == GENERIC_AUTH_ERROR

    async def test_lock_writes_account_locked_audit_entry(
        self, client: AsyncClient, tenant: Tenant, account, db: AsyncSession
    ) -> None:
        for _ in range(LOGIN_LOCKOUT_THRESHOLD):
            r = await _attempt(client, tenant.id, WRONG_PASSWORD)
            assert r.status_code == 401, r.text

        rows = (
            (await db.execute(select(AuditLog).where(AuditLog.action == "account.locked")))
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].tenant_id == tenant.id
        assert rows[0].resource_id == str(account.id)


class TestLockExpiry:
    async def test_after_lock_expiry_correct_login_works_and_resets_counter(
        self, client: AsyncClient, tenant: Tenant, account, db: AsyncSession
    ) -> None:
        for _ in range(LOGIN_LOCKOUT_THRESHOLD):
            r = await _attempt(client, tenant.id, WRONG_PASSWORD)
            assert r.status_code == 401, r.text

        # ``account`` is already identity-mapped in this session — a plain
        # select() would return the STALE cached instance, not the row the
        # app's (separate) session just committed. db.refresh() re-fetches.
        await db.refresh(account)
        assert account.locked_until is not None

        # Simulate the 5 min lock having expired.
        account.locked_until = datetime.now(tz=UTC) - timedelta(minutes=1)
        db.add(account)
        await db.commit()

        r = await _attempt(client, tenant.id, CORRECT_PASSWORD)
        assert r.status_code == 200, r.text

        await db.refresh(account)
        assert account.failed_login_count == 0
        assert account.locked_until is None


class TestSuccessResetsCounter:
    async def test_successful_login_resets_failed_count(
        self, client: AsyncClient, tenant: Tenant, account, db: AsyncSession
    ) -> None:
        # A few wrong attempts, well below the lockout threshold.
        for _ in range(3):
            r = await _attempt(client, tenant.id, WRONG_PASSWORD)
            assert r.status_code == 401, r.text

        await db.refresh(account)
        assert account.failed_login_count == 3
        assert account.locked_until is None

        r = await _attempt(client, tenant.id, CORRECT_PASSWORD)
        assert r.status_code == 200, r.text

        await db.refresh(account)
        assert account.failed_login_count == 0
        assert account.locked_until is None
