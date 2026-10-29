"""Tests for POST /api/v1/clock/nfc/register-to-pool (device-authenticated).

Pilot support for on-terminal DESFire provisioning (Epic eby / bead im7): the
provisioning script reports freshly personalized token-ids so they appear in
the tenant's scan pool immediately, without a per-fob terminal tap.

Uses the real DB-backed device-key auth (``get_device`` / ``Device`` model,
see ``test_device_auth.py``): the tenant is resolved from the device's own
registration, never from a client-supplied header alone.
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


def _url() -> str:
    return "/api/v1/clock/nfc/register-to-pool"


class TestRegisterToPool:
    async def test_valid_device_key_pools_batch(
        self, client: AsyncClient, tenant: Tenant, db: AsyncSession
    ) -> None:
        _device, plaintext = await _create_device(db, tenant.id)
        await db.commit()

        r = await client.post(
            _url(),
            json={"token_ids": ["04AABBCC001122", "04AABBCC003344"]},
            headers={"X-Device-Api-Key": plaintext},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["pooled"] == 2
        assert body["skipped_existing"] == 0
        assert body["skipped_assigned"] == 0
        assert body["pool_size"] == 2

    async def test_pooled_tokens_appear_in_scanned_cards(
        self, client: AsyncClient, tenant: Tenant, employee: User, db: AsyncSession
    ) -> None:
        from zeiterfassung.core.security import create_access_token

        _device, plaintext = await _create_device(db, tenant.id)
        await db.commit()

        r = await client.post(
            _url(),
            json={"token_ids": ["04AABBCC005566"]},
            headers={"X-Device-Api-Key": plaintext},
        )
        assert r.status_code == 200, r.text

        from tests.conftest import ensure_login_account

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
        assert "04AABBCC005566" in uids

    async def test_missing_device_key_401(self, client: AsyncClient, tenant: Tenant) -> None:
        r = await client.post(
            _url(),
            json={"token_ids": ["04AABBCC007788"]},
        )
        assert r.status_code == 401

    async def test_invalid_device_key_401(self, client: AsyncClient, tenant: Tenant) -> None:
        r = await client.post(
            _url(),
            json={"token_ids": ["04AABBCC009900"]},
            headers={"X-Device-Api-Key": "not-a-real-key"},
        )
        assert r.status_code == 401

    async def test_x_tenant_id_mismatch_is_403(
        self, client: AsyncClient, tenant: Tenant, other_tenant: Tenant, db: AsyncSession
    ) -> None:
        _device, plaintext = await _create_device(db, tenant.id)
        await db.commit()

        r = await client.post(
            _url(),
            json={"token_ids": ["04AABBCC00CCDD"]},
            headers={"X-Device-Api-Key": plaintext, "X-Tenant-Id": str(other_tenant.id)},
        )
        assert r.status_code == 403

    async def test_x_tenant_id_matching_is_allowed(
        self, client: AsyncClient, tenant: Tenant, db: AsyncSession
    ) -> None:
        _device, plaintext = await _create_device(db, tenant.id)
        await db.commit()

        r = await client.post(
            _url(),
            json={"token_ids": ["04AABBCC00DDEE"]},
            headers={"X-Device-Api-Key": plaintext, "X-Tenant-Id": str(tenant.id)},
        )
        assert r.status_code == 200, r.text
        assert r.json()["pooled"] == 1

    async def test_token_already_assigned_is_not_pooled(
        self, client: AsyncClient, tenant: Tenant, employee: User, db: AsyncSession
    ) -> None:
        # `employee` fixture has nfc_badge_id="BADGE001" and is active.
        _device, plaintext = await _create_device(db, tenant.id)
        await db.commit()

        r = await client.post(
            _url(),
            json={"token_ids": ["BADGE001", "04AABBCC00EEFF"]},
            headers={"X-Device-Api-Key": plaintext},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["pooled"] == 1
        assert body["skipped_assigned"] == 1
        assert body["pool_size"] == 1

    async def test_reposting_same_batch_does_not_duplicate(
        self, client: AsyncClient, tenant: Tenant, db: AsyncSession
    ) -> None:
        _device, plaintext = await _create_device(db, tenant.id)
        await db.commit()
        headers = {"X-Device-Api-Key": plaintext}

        payload = {"token_ids": ["04AABBCC00FF11", "04AABBCC00FF22"]}
        r1 = await client.post(_url(), json=payload, headers=headers)
        assert r1.status_code == 200, r1.text
        assert r1.json()["pooled"] == 2

        r2 = await client.post(_url(), json=payload, headers=headers)
        assert r2.status_code == 200, r2.text
        body2 = r2.json()
        assert body2["pooled"] == 0
        assert body2["skipped_existing"] == 2
        assert body2["pool_size"] == 2

    async def test_in_batch_duplicate_token_id_not_double_pooled(
        self, client: AsyncClient, tenant: Tenant, db: AsyncSession
    ) -> None:
        _device, plaintext = await _create_device(db, tenant.id)
        await db.commit()

        r = await client.post(
            _url(),
            json={"token_ids": ["04AABBCC00FF33", "04AABBCC00FF33"]},
            headers={"X-Device-Api-Key": plaintext},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["pooled"] == 1
        assert body["skipped_existing"] == 1
        assert body["pool_size"] == 1

    async def test_empty_batch_rejected(
        self, client: AsyncClient, tenant: Tenant, db: AsyncSession
    ) -> None:
        _device, plaintext = await _create_device(db, tenant.id)
        await db.commit()

        r = await client.post(
            _url(), json={"token_ids": []}, headers={"X-Device-Api-Key": plaintext}
        )
        assert r.status_code == 422

    async def test_oversized_batch_rejected(
        self, client: AsyncClient, tenant: Tenant, db: AsyncSession
    ) -> None:
        _device, plaintext = await _create_device(db, tenant.id)
        await db.commit()

        r = await client.post(
            _url(),
            json={"token_ids": [f"04{i:012X}" for i in range(501)]},
            headers={"X-Device-Api-Key": plaintext},
        )
        assert r.status_code == 422

    async def test_token_too_short_rejected(
        self, client: AsyncClient, tenant: Tenant, db: AsyncSession
    ) -> None:
        _device, plaintext = await _create_device(db, tenant.id)
        await db.commit()

        r = await client.post(
            _url(), json={"token_ids": ["abc"]}, headers={"X-Device-Api-Key": plaintext}
        )
        assert r.status_code == 422


class TestRegisterToPoolChipUid:
    """Bead 8t0: chip-UID (physical, phone-readable) stored alongside the token."""

    async def test_register_with_uid_stores_chip_uid(
        self, client: AsyncClient, tenant: Tenant, db: AsyncSession
    ) -> None:
        _device, plaintext = await _create_device(db, tenant.id)
        await db.commit()

        r = await client.post(
            _url(),
            json={
                "token_ids": ["04AABBCC010101"],
                "uids": ["04aabbccddeeff"],
            },
            headers={"X-Device-Api-Key": plaintext},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["pooled"] == 1
        assert body["backfilled"] == 0

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
        entry = next(c for c in r2.json() if c["uid"] == "04AABBCC010101")
        # Normalized to upper-case for consistent matching against phone-read UIDs.
        assert entry["chip_uid"] == "04AABBCCDDEEFF"

    async def test_register_without_uid_is_backward_compatible(
        self, client: AsyncClient, tenant: Tenant, db: AsyncSession
    ) -> None:
        _device, plaintext = await _create_device(db, tenant.id)
        await db.commit()

        r = await client.post(
            _url(),
            json={"token_ids": ["04AABBCC020202"]},
            headers={"X-Device-Api-Key": plaintext},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["pooled"] == 1
        assert body["backfilled"] == 0

    async def test_backfill_adds_chip_uid_to_existing_pool_entry_without_duplicate(
        self, client: AsyncClient, tenant: Tenant, db: AsyncSession
    ) -> None:
        _device, plaintext = await _create_device(db, tenant.id)
        await db.commit()
        headers = {"X-Device-Api-Key": plaintext}

        r1 = await client.post(
            _url(),
            json={"token_ids": ["04AABBCC030303"]},
            headers=headers,
        )
        assert r1.status_code == 200, r1.text
        assert r1.json()["pooled"] == 1

        r2 = await client.post(
            _url(),
            json={"token_ids": ["04AABBCC030303"], "uids": ["1122334455"]},
            headers=headers,
        )
        assert r2.status_code == 200, r2.text
        body2 = r2.json()
        assert body2["pooled"] == 0
        assert body2["skipped_existing"] == 1
        assert body2["backfilled"] == 1
        assert body2["pool_size"] == 1

        from tests.conftest import ensure_login_account
        from zeiterfassung.core.security import create_access_token

        acc = await ensure_login_account(db, tenant.id)
        token = create_access_token(
            user_id=str(acc.id),
            tenant_id=str(tenant.id),
            permissions=["admin:dashboard"],
            data_scope="global",
        )
        r3 = await client.get(
            "/api/v1/admin/scanned-cards",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r3.status_code == 200, r3.text
        matches = [c for c in r3.json() if c["uid"] == "04AABBCC030303"]
        assert len(matches) == 1
        assert matches[0]["chip_uid"] == "1122334455".upper()

    async def test_backfill_does_not_overwrite_existing_chip_uid(
        self, client: AsyncClient, tenant: Tenant, db: AsyncSession
    ) -> None:
        _device, plaintext = await _create_device(db, tenant.id)
        await db.commit()
        headers = {"X-Device-Api-Key": plaintext}

        r1 = await client.post(
            _url(),
            json={"token_ids": ["04AABBCC040404"], "uids": ["AAAAAAAA"]},
            headers=headers,
        )
        assert r1.status_code == 200, r1.text

        # Re-post with a *different* chip_uid — must NOT overwrite, and must
        # not count as backfilled (nothing was missing to fill in).
        r2 = await client.post(
            _url(),
            json={"token_ids": ["04AABBCC040404"], "uids": ["BBBBBBBB"]},
            headers=headers,
        )
        assert r2.status_code == 200, r2.text
        body2 = r2.json()
        assert body2["skipped_existing"] == 1
        assert body2["backfilled"] == 0

        from tests.conftest import ensure_login_account
        from zeiterfassung.core.security import create_access_token

        acc = await ensure_login_account(db, tenant.id)
        token = create_access_token(
            user_id=str(acc.id),
            tenant_id=str(tenant.id),
            permissions=["admin:dashboard"],
            data_scope="global",
        )
        r3 = await client.get(
            "/api/v1/admin/scanned-cards",
            headers={"Authorization": f"Bearer {token}"},
        )
        entry = next(c for c in r3.json() if c["uid"] == "04AABBCC040404")
        assert entry["chip_uid"] == "AAAAAAAA"

    async def test_uids_length_mismatch_rejected(
        self, client: AsyncClient, tenant: Tenant, db: AsyncSession
    ) -> None:
        _device, plaintext = await _create_device(db, tenant.id)
        await db.commit()

        r = await client.post(
            _url(),
            json={
                "token_ids": ["04AABBCC050505", "04AABBCC060606"],
                "uids": ["AABBCCDD"],
            },
            headers={"X-Device-Api-Key": plaintext},
        )
        assert r.status_code == 422

    async def test_uid_too_short_rejected(
        self, client: AsyncClient, tenant: Tenant, db: AsyncSession
    ) -> None:
        _device, plaintext = await _create_device(db, tenant.id)
        await db.commit()

        r = await client.post(
            _url(),
            json={"token_ids": ["04AABBCC070707"], "uids": ["AABB"]},
            headers={"X-Device-Api-Key": plaintext},
        )
        assert r.status_code == 422

    async def test_uid_non_hex_rejected(
        self, client: AsyncClient, tenant: Tenant, db: AsyncSession
    ) -> None:
        _device, plaintext = await _create_device(db, tenant.id)
        await db.commit()

        r = await client.post(
            _url(),
            json={"token_ids": ["04AABBCC080808"], "uids": ["ZZZZZZZZ"]},
            headers={"X-Device-Api-Key": plaintext},
        )
        assert r.status_code == 422

    async def test_uids_with_null_entry_allowed(
        self, client: AsyncClient, tenant: Tenant, db: AsyncSession
    ) -> None:
        _device, plaintext = await _create_device(db, tenant.id)
        await db.commit()

        r = await client.post(
            _url(),
            json={
                "token_ids": ["04AABBCC090909", "04AABBCC0A0A0A"],
                "uids": [None, "1122334455"],
            },
            headers={"X-Device-Api-Key": plaintext},
        )
        assert r.status_code == 200, r.text
        assert r.json()["pooled"] == 2
