"""Tests for admin-fob-gated terminal personalize mode.

Covers:
- ``POST /api/v1/clock/nfc/register-admin-cards`` (flag token-ids as admin
  fobs, device-authenticated).
- ``POST /api/v1/clock/nfc/preview``'s ``is_admin`` field, including the
  "admin-only fob with no assigned employee" branch (resolves instead of
  404ing) and the "employee badge that is also admin-flagged" branch.

Mirrors the device-auth patterns in ``test_register_to_pool.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from zeiterfassung.api.deps import hash_device_key
from zeiterfassung.models.device import Device
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
    async with AsyncClient(transport=ASGITransport(app=app_with_db), base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def other_tenant(db: AsyncSession) -> Tenant:
    now = datetime.now(tz=UTC)
    t = Tenant(
        name="Other GmbH",
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


async def _create_device(
    db: AsyncSession, tenant_id: uuid.UUID, name: str = "Terminal 1", is_active: bool = True
) -> tuple[Device, str]:
    plaintext = f"zdk_test_{uuid.uuid4().hex}"
    now = datetime.now(tz=UTC)
    device = Device(
        tenant_id=tenant_id,
        name=name,
        key_hash=hash_device_key(plaintext),
        is_active=is_active,
        created_at=now,
        updated_at=now,
    )
    db.add(device)
    await db.flush()
    return device, plaintext


def _admin_url() -> str:
    return "/api/v1/clock/nfc/register-admin-cards"


def _preview_url() -> str:
    return "/api/v1/clock/nfc/preview"


def _pool_url() -> str:
    return "/api/v1/clock/nfc/register-to-pool"


async def _pool(client: AsyncClient, headers: dict[str, str], *token_ids: str) -> None:
    """Pre-pool token-ids via register-to-pool — since register-admin-cards
    now requires a token to already be "known" (pooled or an assigned
    employee badge) before it can be flagged admin."""
    r = await client.post(_pool_url(), json={"token_ids": list(token_ids)}, headers=headers)
    assert r.status_code == 200, r.text


class TestRegisterAdminCards:
    async def test_valid_device_key_flags_batch(
        self, client: AsyncClient, tenant: Tenant, db: AsyncSession
    ) -> None:
        _device, plaintext = await _create_device(db, tenant.id)
        await db.commit()
        headers = {"X-Device-Api-Key": plaintext}
        await _pool(client, headers, "04ADMIN0001122", "04ADMIN0003344")

        r = await client.post(
            _admin_url(),
            json={"token_ids": ["04ADMIN0001122", "04ADMIN0003344"]},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["added"] == 2
        assert body["already_admin"] == 0
        assert body["admin_count"] == 2

    async def test_missing_device_key_401(self, client: AsyncClient, tenant: Tenant) -> None:
        r = await client.post(_admin_url(), json={"token_ids": ["04ADMIN0007788"]})
        assert r.status_code == 401

    async def test_x_tenant_id_mismatch_is_403(
        self, client: AsyncClient, tenant: Tenant, other_tenant: Tenant, db: AsyncSession
    ) -> None:
        _device, plaintext = await _create_device(db, tenant.id)
        await db.commit()

        r = await client.post(
            _admin_url(),
            json={"token_ids": ["04ADMIN00CCDD"]},
            headers={"X-Device-Api-Key": plaintext, "X-Tenant-Id": str(other_tenant.id)},
        )
        assert r.status_code == 403

    async def test_reposting_same_batch_does_not_duplicate(
        self, client: AsyncClient, tenant: Tenant, db: AsyncSession
    ) -> None:
        _device, plaintext = await _create_device(db, tenant.id)
        await db.commit()
        headers = {"X-Device-Api-Key": plaintext}
        await _pool(client, headers, "04ADMIN00FF11", "04ADMIN00FF22")

        payload = {"token_ids": ["04ADMIN00FF11", "04ADMIN00FF22"]}
        r1 = await client.post(_admin_url(), json=payload, headers=headers)
        assert r1.status_code == 200, r1.text
        assert r1.json()["added"] == 2

        r2 = await client.post(_admin_url(), json=payload, headers=headers)
        assert r2.status_code == 200, r2.text
        body2 = r2.json()
        assert body2["added"] == 0
        assert body2["already_admin"] == 2
        assert body2["admin_count"] == 2

    async def test_empty_batch_rejected(
        self, client: AsyncClient, tenant: Tenant, db: AsyncSession
    ) -> None:
        _device, plaintext = await _create_device(db, tenant.id)
        await db.commit()

        r = await client.post(
            _admin_url(), json={"token_ids": []}, headers={"X-Device-Api-Key": plaintext}
        )
        assert r.status_code == 422

    async def test_oversized_batch_rejected(
        self, client: AsyncClient, tenant: Tenant, db: AsyncSession
    ) -> None:
        _device, plaintext = await _create_device(db, tenant.id)
        await db.commit()

        r = await client.post(
            _admin_url(),
            json={"token_ids": [f"04{i:012X}" for i in range(51)]},
            headers={"X-Device-Api-Key": plaintext},
        )
        assert r.status_code == 422

    async def test_admin_cards_never_pooled_as_employee_candidates(
        self, client: AsyncClient, tenant: Tenant, db: AsyncSession
    ) -> None:
        """Admin fobs must never show up in the scanned-cards pool."""
        _device, plaintext = await _create_device(db, tenant.id)
        await db.commit()
        headers = {"X-Device-Api-Key": plaintext}
        await _pool(client, headers, "04ADMINPOOLCHK")

        r = await client.post(_admin_url(), json={"token_ids": ["04ADMINPOOLCHK"]}, headers=headers)
        assert r.status_code == 200, r.text

        from tests.conftest import ensure_login_account
        from zeiterfassung.core.security import create_access_token

        acc = await ensure_login_account(db, tenant.id)
        token = create_access_token(
            user_id=str(acc.id),
            tenant_id=str(tenant.id),
            permissions=["admin:dashboard"],
            data_scope="global",
        )
        r2 = await client.get(
            "/api/v1/admin/scanned-cards",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r2.status_code == 200, r2.text
        uids = {c["uid"] for c in r2.json()}
        assert "04ADMINPOOLCHK" not in uids

    async def test_unknown_token_id_rejected_400(
        self, client: AsyncClient, tenant: Tenant, db: AsyncSession
    ) -> None:
        """A token that was never personalized+pooled (or assigned as an
        employee badge) must not be flaggable as an admin fob."""
        _device, plaintext = await _create_device(db, tenant.id)
        await db.commit()
        headers = {"X-Device-Api-Key": plaintext}

        r = await client.post(_admin_url(), json={"token_ids": ["04NEVERSCANNED"]}, headers=headers)
        assert r.status_code == 400, r.text

    async def test_pooled_token_id_accepted(
        self, client: AsyncClient, tenant: Tenant, db: AsyncSession
    ) -> None:
        _device, plaintext = await _create_device(db, tenant.id)
        await db.commit()
        headers = {"X-Device-Api-Key": plaintext}
        await _pool(client, headers, "04REALLYPOOLED")

        r = await client.post(_admin_url(), json={"token_ids": ["04REALLYPOOLED"]}, headers=headers)
        assert r.status_code == 200, r.text
        assert r.json()["added"] == 1

    async def test_mixed_batch_with_one_unknown_token_id_rejected(
        self, client: AsyncClient, tenant: Tenant, db: AsyncSession
    ) -> None:
        """A batch containing even one never-scanned token must be rejected
        wholesale (no partial admin-flagging)."""
        _device, plaintext = await _create_device(db, tenant.id)
        await db.commit()
        headers = {"X-Device-Api-Key": plaintext}
        await _pool(client, headers, "04MIXPOOLED")

        r = await client.post(
            _admin_url(),
            json={"token_ids": ["04MIXPOOLED", "04MIXNEVERSCANNED"]},
            headers=headers,
        )
        assert r.status_code == 400, r.text

    async def test_flagged_admin_token_removed_from_scanned_pool(
        self, client: AsyncClient, tenant: Tenant, db: AsyncSession
    ) -> None:
        """Once flagged admin, the token disappears from the scanned pool —
        even though it had to be pooled first to pass the membership check."""
        _device, plaintext = await _create_device(db, tenant.id)
        await db.commit()
        headers = {"X-Device-Api-Key": plaintext}
        await _pool(client, headers, "04POOLTHENADMIN")

        r = await client.post(
            _admin_url(), json={"token_ids": ["04POOLTHENADMIN"]}, headers=headers
        )
        assert r.status_code == 200, r.text

        from tests.conftest import ensure_login_account
        from zeiterfassung.core.security import create_access_token

        acc = await ensure_login_account(db, tenant.id)
        token = create_access_token(
            user_id=str(acc.id),
            tenant_id=str(tenant.id),
            permissions=["admin:dashboard"],
            data_scope="global",
        )
        r2 = await client.get(
            "/api/v1/admin/scanned-cards",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r2.status_code == 200, r2.text
        uids = {c["uid"] for c in r2.json()}
        assert "04POOLTHENADMIN" not in uids


class TestPreviewIsAdmin:
    async def test_normal_employee_badge_is_admin_false(
        self, client: AsyncClient, tenant: Tenant, employee: User, db: AsyncSession
    ) -> None:
        _device, plaintext = await _create_device(db, tenant.id)
        await db.commit()

        r = await client.post(
            _preview_url(),
            json={"badge_id": employee.nfc_badge_id},
            headers={"X-Device-Api-Key": plaintext},
        )
        assert r.status_code == 200, r.text
        assert r.json()["is_admin"] is False

    async def test_unknown_badge_still_404s_when_not_admin(
        self, client: AsyncClient, tenant: Tenant, db: AsyncSession
    ) -> None:
        _device, plaintext = await _create_device(db, tenant.id)
        await db.commit()

        r = await client.post(
            _preview_url(),
            json={"badge_id": "TOTALLY-UNKNOWN"},
            headers={"X-Device-Api-Key": plaintext},
        )
        assert r.status_code == 404

    async def test_admin_only_fob_resolves_without_employee(
        self, client: AsyncClient, tenant: Tenant, db: AsyncSession
    ) -> None:
        """An admin fob with no assigned employee must resolve (not 404) and
        carry ``is_admin: true`` with placeholder employee/state fields."""
        _device, plaintext = await _create_device(db, tenant.id)
        await db.commit()
        headers = {"X-Device-Api-Key": plaintext}
        await _pool(client, headers, "04ADMINONLYFOB")

        r_mint = await client.post(
            _admin_url(), json={"token_ids": ["04ADMINONLYFOB"]}, headers=headers
        )
        assert r_mint.status_code == 200, r_mint.text

        r = await client.post(_preview_url(), json={"badge_id": "04ADMINONLYFOB"}, headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["is_admin"] is True
        assert body["employee_name"] == ""

    async def test_employee_badge_also_flagged_admin_returns_both(
        self, client: AsyncClient, tenant: Tenant, employee: User, db: AsyncSession
    ) -> None:
        """A supervisor's own badge can double as an admin fob: normal
        employee fields PLUS ``is_admin: true``."""
        _device, plaintext = await _create_device(db, tenant.id)
        await db.commit()
        headers = {"X-Device-Api-Key": plaintext}

        r_mint = await client.post(
            _admin_url(), json={"token_ids": [employee.nfc_badge_id]}, headers=headers
        )
        assert r_mint.status_code == 200, r_mint.text

        r = await client.post(
            _preview_url(), json={"badge_id": employee.nfc_badge_id}, headers=headers
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["is_admin"] is True
        assert body["employee_name"] != ""

    async def test_admin_flag_does_not_leak_across_tenants(
        self,
        client: AsyncClient,
        tenant: Tenant,
        other_tenant: Tenant,
        db: AsyncSession,
    ) -> None:
        _device_a, plaintext_a = await _create_device(db, tenant.id)
        _device_b, plaintext_b = await _create_device(db, other_tenant.id, name="Terminal B")
        await db.commit()
        await _pool(client, {"X-Device-Api-Key": plaintext_a}, "04CROSSTENANT1")

        r_mint = await client.post(
            _admin_url(),
            json={"token_ids": ["04CROSSTENANT1"]},
            headers={"X-Device-Api-Key": plaintext_a},
        )
        assert r_mint.status_code == 200, r_mint.text

        # Same token tapped on the OTHER tenant's device must not be admin.
        r = await client.post(
            _preview_url(),
            json={"badge_id": "04CROSSTENANT1"},
            headers={"X-Device-Api-Key": plaintext_b},
        )
        assert r.status_code == 404
