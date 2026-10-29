"""Sofortige Token-Entwertung via accounts.token_version (login-hardening).

Spec: docs/superpowers/specs/2026-07-20-login-hardening-design.md Abschnitt 1.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from zeiterfassung.core.security import create_access_token, decode_access_token, hash_password
from zeiterfassung.models.account import Account
from zeiterfassung.models.tenant import Tenant


@pytest.mark.anyio
async def test_account_token_version_defaults_to_zero(db: AsyncSession, tenant: Tenant) -> None:
    now = datetime.now(tz=UTC)
    acc = Account(
        tenant_id=tenant.id,
        email=f"tv-{uuid.uuid4().hex[:8]}@test.de",
        hashed_password=hash_password("Passw0rd!"),
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    db.add(acc)
    await db.flush()
    assert acc.token_version == 0


def test_access_token_carries_ver_claim() -> None:
    token = create_access_token(
        user_id=str(uuid.uuid4()),
        tenant_id=str(uuid.uuid4()),
        token_version=3,
    )
    assert decode_access_token(token)["ver"] == 3


def test_access_token_ver_defaults_to_zero() -> None:
    token = create_access_token(user_id=str(uuid.uuid4()), tenant_id=str(uuid.uuid4()))
    assert decode_access_token(token)["ver"] == 0


@pytest_asyncio.fixture
async def app_with_db(engine):
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
    # https-Base, damit der Secure-Refresh-Cookie im Cookie-Jar landet.
    async with AsyncClient(transport=ASGITransport(app=app_with_db), base_url="https://test") as c:
        yield c


@pytest_asyncio.fixture
async def account(db: AsyncSession, tenant: Tenant) -> Account:
    now = datetime.now(tz=UTC)
    acc = Account(
        tenant_id=tenant.id,
        email="revocation@test.de",
        hashed_password=hash_password("Passw0rd!"),
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    db.add(acc)
    await db.flush()
    await db.commit()
    return acc


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _mint(account: Account, ver: int | None) -> str:
    kwargs = {} if ver is None else {"token_version": ver}
    return create_access_token(user_id=str(account.id), tenant_id=str(account.tenant_id), **kwargs)


@pytest.mark.anyio
async def test_token_of_unknown_account_is_rejected(client, tenant) -> None:
    token = create_access_token(user_id=str(uuid.uuid4()), tenant_id=str(tenant.id))
    resp = await client.get("/api/v1/auth/me", headers=_headers(token))
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_token_of_inactive_account_is_rejected(client, db, account) -> None:
    token = _mint(account, ver=0)
    account.is_active = False
    await db.commit()
    resp = await client.get("/api/v1/auth/me", headers=_headers(token))
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_token_with_stale_version_is_rejected(client, db, account) -> None:
    token = _mint(account, ver=0)
    account.token_version = 1
    await db.commit()
    resp = await client.get("/api/v1/auth/me", headers=_headers(token))
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_token_with_current_version_is_accepted(client, account) -> None:
    resp = await client.get("/api/v1/auth/me", headers=_headers(_mint(account, ver=0)))
    assert resp.status_code == 200


@pytest.mark.anyio
async def test_legacy_token_without_ver_claim_counts_as_zero(client, account) -> None:
    # Alt-Token vor dem Rollout: gar kein ver-Claim -> gilt als 0 (Spec,
    # Rollout: kein Zwangs-Logout). Direkt ueber _create_token gebaut, weil
    # create_access_token inzwischen immer ver schreibt.
    from zeiterfassung.core.security import _create_token

    legacy = _create_token(
        subject=str(account.id),
        token_type="access",
        extra_claims={
            "tenant_id": str(account.tenant_id),
            "permissions": [],
            "data_scope": "self",
            "is_superadmin": False,
        },
    )
    resp = await client.get("/api/v1/auth/me", headers=_headers(legacy))
    assert resp.status_code == 200


async def _login(client, email: str, password: str = "Passw0rd!"):
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200
    return resp.json()["access_token"], client.cookies.get("zeit_refresh")


@pytest.mark.anyio
async def test_logout_invalidates_access_token_immediately(client, account) -> None:
    access, _ = await _login(client, account.email)
    assert (await client.get("/api/v1/auth/me", headers=_headers(access))).status_code == 200
    assert (await client.post("/api/v1/auth/logout")).status_code == 204
    # Vorher: Token waere noch bis zu 15 min gueltig gewesen. Jetzt: sofort 401.
    assert (await client.get("/api/v1/auth/me", headers=_headers(access))).status_code == 401


@pytest.mark.anyio
async def test_second_device_recovers_via_refresh_after_logout(client, account) -> None:
    # Geraet B loggt sich ein, dann loggt Geraet A (frischer Login) sich aus.
    access_b, refresh_b = await _login(client, account.email)
    _access_a, _ = await _login(client, account.email)
    assert (await client.post("/api/v1/auth/logout")).status_code == 204

    # B's Access-Token ist tot (Version-Bump) ...
    assert (await client.get("/api/v1/auth/me", headers=_headers(access_b))).status_code == 401
    # ... aber B's Refresh-Familie lebt: Refresh liefert einen frischen Token.
    client.cookies.set("zeit_refresh", refresh_b)
    resp = await client.post("/api/v1/auth/refresh")
    assert resp.status_code == 200
    fresh = resp.json()["access_token"]
    assert (await client.get("/api/v1/auth/me", headers=_headers(fresh))).status_code == 200


@pytest.mark.anyio
async def test_password_change_invalidates_all_tokens_and_sessions(client, db, account) -> None:
    access_old, refresh_old = await _login(client, account.email)
    access_cur, _ = await _login(client, account.email)

    resp = await client.post(
        "/api/v1/auth/change-password",
        headers=_headers(access_cur),
        json={"current_password": "Passw0rd!", "new_password": "NeuesPw123!"},
    )
    assert resp.status_code == 204

    # Beide Access-Tokens sofort tot (auch der des Aufrufers — Re-Login noetig) ...
    assert (await client.get("/api/v1/auth/me", headers=_headers(access_old))).status_code == 401
    assert (await client.get("/api/v1/auth/me", headers=_headers(access_cur))).status_code == 401
    # ... und ALLE Refresh-Sessions revoked: gestohlener Cookie ueberlebt den
    # Passwortwechsel nicht.
    client.cookies.set("zeit_refresh", refresh_old)
    assert (await client.post("/api/v1/auth/refresh")).status_code == 401


@pytest.mark.anyio
async def test_archive_user_invalidates_linked_account(
    client, db, tenant, employee, account
) -> None:
    from zeiterfassung.services import user_service

    account.employee_id = employee.id
    await db.commit()
    access, _ = await _login(client, account.email)
    assert (await client.get("/api/v1/auth/me", headers=_headers(access))).status_code == 200

    await user_service.archive_user(db, employee)
    await db.commit()
    assert (await client.get("/api/v1/auth/me", headers=_headers(access))).status_code == 401
