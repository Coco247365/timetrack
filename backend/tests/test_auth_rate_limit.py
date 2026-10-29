"""In-process Rate-Limiting auf /auth/login und /auth/refresh (login-hardening).

Spec: docs/superpowers/specs/2026-07-20-login-hardening-design.md Abschnitt 2.
Der autouse-Reset der Buckets liegt in conftest.py.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from zeiterfassung.config import get_settings
from zeiterfassung.core.security import hash_password
from zeiterfassung.models.account import Account
from zeiterfassung.models.tenant import Tenant


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
    async with AsyncClient(transport=ASGITransport(app=app_with_db), base_url="https://test") as c:
        yield c


@pytest_asyncio.fixture
async def account(db: AsyncSession, tenant: Tenant) -> Account:
    now = datetime.now(tz=UTC)
    acc = Account(
        tenant_id=tenant.id,
        email="ratelimit@test.de",
        hashed_password=hash_password("Passw0rd!"),
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    db.add(acc)
    await db.flush()
    await db.commit()
    return acc


async def _try_login(client, email: str) -> int:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": "falsch!"})
    return resp.status_code


@pytest.mark.anyio
async def test_login_ip_limit_returns_429_with_retry_after(client, account, monkeypatch) -> None:
    s = get_settings()
    monkeypatch.setattr(s, "auth_rate_limit_login_ip_max", 3)
    monkeypatch.setattr(s, "auth_rate_limit_login_email_max", 1000)
    # Pro Versuch andere E-Mail -> nur der IP-Schluessel zaehlt hoch.
    for i in range(3):
        assert await _try_login(client, f"user{i}@test.de") == 401
    resp = await client.post(
        "/api/v1/auth/login", json={"email": "user99@test.de", "password": "x"}
    )
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers


@pytest.mark.anyio
async def test_login_email_limit_is_case_insensitive(client, account, monkeypatch) -> None:
    s = get_settings()
    monkeypatch.setattr(s, "auth_rate_limit_login_ip_max", 1000)
    monkeypatch.setattr(s, "auth_rate_limit_login_email_max", 3)
    for variant in ("Rate@test.de", "rate@test.de", "RATE@test.de"):
        assert await _try_login(client, variant) == 401
    assert await _try_login(client, "rate@TEST.de") == 429


@pytest.mark.anyio
async def test_window_expiry_frees_the_bucket(client, account, monkeypatch) -> None:
    from zeiterfassung.core import rate_limit

    s = get_settings()
    monkeypatch.setattr(s, "auth_rate_limit_login_ip_max", 2)
    monkeypatch.setattr(s, "auth_rate_limit_login_email_max", 1000)

    fake_now = [1000.0]
    monkeypatch.setattr(rate_limit, "_now", lambda: fake_now[0])

    assert await _try_login(client, "w1@test.de") == 401
    assert await _try_login(client, "w2@test.de") == 401
    assert await _try_login(client, "w3@test.de") == 429
    # Fenster (60 s) verstreichen lassen -> wieder frei.
    fake_now[0] += 61.0
    assert await _try_login(client, "w4@test.de") == 401


@pytest.mark.anyio
async def test_refresh_ip_limit(client, monkeypatch) -> None:
    s = get_settings()
    monkeypatch.setattr(s, "auth_rate_limit_refresh_ip_max", 2)
    # Ohne Cookie antwortet /auth/refresh mit 401 — bis das Limit greift.
    for _ in range(2):
        assert (await client.post("/api/v1/auth/refresh")).status_code == 401
    assert (await client.post("/api/v1/auth/refresh")).status_code == 429


@pytest.mark.anyio
async def test_successful_login_still_works_within_limits(client, account) -> None:
    resp = await client.post(
        "/api/v1/auth/login", json={"email": account.email, "password": "Passw0rd!"}
    )
    assert resp.status_code == 200


def test_prune_drops_stale_buckets(monkeypatch) -> None:
    from zeiterfassung.core import rate_limit

    fake_now = [1000.0]
    monkeypatch.setattr(rate_limit, "_now", lambda: fake_now[0])
    monkeypatch.setattr(rate_limit, "_PRUNE_THRESHOLD", 5)

    rate_limit.reset()
    for i in range(6):
        rate_limit.check(f"k{i}", 10, 60.0)
    assert len(rate_limit._buckets) == 6

    # Fenster verstreichen lassen -> naechster check() prunt die alten Keys.
    fake_now[0] += 61.0
    rate_limit.check("fresh", 10, 60.0)
    assert set(rate_limit._buckets) == {"fresh"}

    rate_limit.reset()
