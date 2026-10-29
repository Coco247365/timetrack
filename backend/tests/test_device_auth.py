"""Tests for real DB-backed NFC terminal device authentication.

Exercises the real dependency/endpoint path:
- ``get_device`` (deps.py) directly against a real DB session.
- The admin device-management endpoints (register/list/revoke) through the
  actual FastAPI app + ASGI transport, authenticated with a real JWT.
- The device-authenticated ``/api/v1/clock/nfc/preview`` kiosk endpoint, to
  prove tenant resolution + the X-Tenant-Id cross-check end-to-end.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from zeiterfassung.api import deps
from zeiterfassung.api.deps import get_device, hash_device_key
from zeiterfassung.config import get_settings
from zeiterfassung.core.security import create_access_token
from zeiterfassung.models.device import Device
from zeiterfassung.models.tenant import Tenant

# ---------------------------------------------------------------------------
# App + client fixtures (same pattern as test_rbac_matrix.py)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def app_with_db(engine):
    """Return a FastAPI app instance with the test DB engine injected."""
    from sqlalchemy.ext.asyncio import AsyncSession as _AS
    from sqlalchemy.ext.asyncio import async_sessionmaker

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


async def _admin_token(db: AsyncSession, tenant_id: uuid.UUID) -> str:
    from tests.conftest import ensure_login_account

    acc = await ensure_login_account(db, tenant_id)
    return create_access_token(
        user_id=str(acc.id),
        tenant_id=str(tenant_id),
        permissions=["admin:dashboard", "admin:settings"],
        role_type="gf",
        data_scope="global",
    )


async def _admin_headers(db: AsyncSession, tenant_id: uuid.UUID) -> dict[str, str]:
    return {"Authorization": f"Bearer {await _admin_token(db, tenant_id)}"}


# ---------------------------------------------------------------------------
# Second tenant fixture — for cross-tenant isolation checks
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def other_tenant(db: AsyncSession) -> Tenant:
    now = datetime.now(tz=UTC)
    t = Tenant(
        name="Other GmbH",
        timezone="Europe/Berlin",
        locale="de-DE",
        bundesland="BE",
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


# ---------------------------------------------------------------------------
# get_device dependency — direct exercise against the real DB
# ---------------------------------------------------------------------------


class TestGetDeviceDependency:
    async def test_valid_key_resolves_correct_tenant(self, db, tenant) -> None:
        device, plaintext = await _create_device(db, tenant.id)

        ctx = await get_device(db=db, x_device_api_key=plaintext)

        assert ctx.tenant_id == tenant.id
        assert ctx.device_id == str(device.id)
        assert ctx.name == device.name

    async def test_valid_key_updates_last_seen_at(self, db, tenant) -> None:
        device, plaintext = await _create_device(db, tenant.id)
        assert device.last_seen_at is None

        await get_device(db=db, x_device_api_key=plaintext)
        await db.flush()

        await db.refresh(device)
        assert device.last_seen_at is not None

    async def test_missing_key_is_401(self, db) -> None:
        with pytest.raises(HTTPException) as exc_info:
            await get_device(db=db, x_device_api_key=None)
        assert exc_info.value.status_code == 401

    async def test_empty_key_is_401(self, db) -> None:
        with pytest.raises(HTTPException) as exc_info:
            await get_device(db=db, x_device_api_key="")
        assert exc_info.value.status_code == 401

    async def test_invalid_key_is_401(self, db, tenant) -> None:
        await _create_device(db, tenant.id)
        with pytest.raises(HTTPException) as exc_info:
            await get_device(db=db, x_device_api_key="not-a-real-key")
        assert exc_info.value.status_code == 401

    async def test_inactive_device_is_401(self, db, tenant) -> None:
        _device, plaintext = await _create_device(db, tenant.id, is_active=False)
        with pytest.raises(HTTPException) as exc_info:
            await get_device(db=db, x_device_api_key=plaintext)
        assert exc_info.value.status_code == 401


# ---------------------------------------------------------------------------
# Admin CRUD endpoints
# ---------------------------------------------------------------------------


class TestAdminDeviceEndpoints:
    async def test_register_returns_key_that_then_authenticates(self, client, db, tenant) -> None:
        resp = await client.post(
            "/api/v1/admin/devices",
            json={"name": "Empfang Terminal"},
            headers=await _admin_headers(db, tenant.id),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["name"] == "Empfang Terminal"
        assert body["is_active"] is True
        assert "key" in body and body["key"].startswith("zdk_")
        assert str(tenant.id) == body["tenant_id"]

        # The returned plaintext key must authenticate via get_device.
        ctx = await get_device(db=db, x_device_api_key=body["key"])
        assert str(ctx.tenant_id) == body["tenant_id"] or ctx.tenant_id == tenant.id
        assert ctx.device_id == body["id"]

    async def test_register_response_never_exposes_key_hash(self, client, db, tenant) -> None:
        resp = await client.post(
            "/api/v1/admin/devices",
            json={"name": "Terminal A"},
            headers=await _admin_headers(db, tenant.id),
        )
        assert resp.status_code == 201
        assert "key_hash" not in resp.json()

    async def test_list_scoped_per_tenant(self, client, db, tenant, other_tenant) -> None:
        await _create_device(db, tenant.id, name="Mine 1")
        await _create_device(db, tenant.id, name="Mine 2")
        await _create_device(db, other_tenant.id, name="Not Mine")
        await db.commit()

        resp = await client.get(
            "/api/v1/admin/devices", headers=await _admin_headers(db, tenant.id)
        )
        assert resp.status_code == 200
        names = {d["name"] for d in resp.json()}
        assert names == {"Mine 1", "Mine 2"}
        assert all("key_hash" not in d for d in resp.json())

    async def test_revoke_then_key_no_longer_authenticates(self, client, db, tenant) -> None:
        device, plaintext = await _create_device(db, tenant.id, name="To Revoke")
        await db.commit()

        # Sanity: key works before revoke.
        ctx = await get_device(db=db, x_device_api_key=plaintext)
        assert ctx.device_id == str(device.id)

        resp = await client.post(
            f"/api/v1/admin/devices/{device.id}/revoke",
            headers=await _admin_headers(db, tenant.id),
        )
        assert resp.status_code == 200
        assert resp.json()["is_active"] is False

        with pytest.raises(HTTPException) as exc_info:
            await get_device(db=db, x_device_api_key=plaintext)
        assert exc_info.value.status_code == 401

    async def test_revoke_wrong_tenant_is_404(self, client, db, tenant, other_tenant) -> None:
        device, _plaintext = await _create_device(db, other_tenant.id, name="Foreign")
        await db.commit()

        resp = await client.post(
            f"/api/v1/admin/devices/{device.id}/revoke",
            headers=await _admin_headers(db, tenant.id),
        )
        assert resp.status_code == 404

    async def test_non_admin_forbidden(self, client, db, tenant) -> None:
        from tests.conftest import ensure_login_account

        acc = await ensure_login_account(db, tenant.id)
        token = create_access_token(
            user_id=str(acc.id),
            tenant_id=str(tenant.id),
            permissions=["timesheet:view_own"],
            role_type="mitarbeiter",
            data_scope="self",
        )
        resp = await client.get(
            "/api/v1/admin/devices", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Device-authenticated kiosk endpoint — tenant resolution + X-Tenant-Id check
# ---------------------------------------------------------------------------


class TestNfcEndpointDeviceAuth:
    async def test_missing_device_key_is_401(self, client) -> None:
        resp = await client.post("/api/v1/clock/nfc/preview", json={"badge_id": "BADGE001"})
        assert resp.status_code == 401

    async def test_invalid_device_key_is_401(self, client) -> None:
        resp = await client.post(
            "/api/v1/clock/nfc/preview",
            json={"badge_id": "BADGE001"},
            headers={"X-Device-Api-Key": "bogus"},
        )
        assert resp.status_code == 401

    async def test_valid_device_key_resolves_tenant_and_badge(
        self, client, db, tenant, employee
    ) -> None:
        _device, plaintext = await _create_device(db, tenant.id)
        await db.commit()

        resp = await client.post(
            "/api/v1/clock/nfc/preview",
            json={"badge_id": employee.nfc_badge_id},
            headers={"X-Device-Api-Key": plaintext},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["employee_name"] == f"{employee.first_name} {employee.last_name}"

    async def test_x_tenant_id_mismatch_is_403(
        self, client, db, tenant, other_tenant, employee
    ) -> None:
        _device, plaintext = await _create_device(db, tenant.id)
        await db.commit()

        resp = await client.post(
            "/api/v1/clock/nfc/preview",
            json={"badge_id": employee.nfc_badge_id},
            headers={
                "X-Device-Api-Key": plaintext,
                "X-Tenant-Id": str(other_tenant.id),
            },
        )
        assert resp.status_code == 403

    async def test_x_tenant_id_matching_is_allowed(self, client, db, tenant, employee) -> None:
        _device, plaintext = await _create_device(db, tenant.id)
        await db.commit()

        resp = await client.post(
            "/api/v1/clock/nfc/preview",
            json={"badge_id": employee.nfc_badge_id},
            headers={
                "X-Device-Api-Key": plaintext,
                "X-Tenant-Id": str(tenant.id),
            },
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# zeiterfassung-2fn — Device.location + device-trust fix + location surfacing
# ---------------------------------------------------------------------------


class TestDeviceLocation:
    async def test_device_location_persists(self, db, tenant) -> None:
        device, _plaintext = await _create_device(db, tenant.id, name="Terminal Halle 2")
        device.location = "Halle 2 / Eingang"
        await db.flush()
        await db.refresh(device)
        assert device.location == "Halle 2 / Eingang"

    async def test_register_device_with_location_round_trips(self, client, db, tenant) -> None:
        resp = await client.post(
            "/api/v1/admin/devices",
            json={"name": "Empfang Terminal", "location": "Empfang EG"},
            headers=await _admin_headers(db, tenant.id),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["location"] == "Empfang EG"

        list_resp = await client.get(
            "/api/v1/admin/devices", headers=await _admin_headers(db, tenant.id)
        )
        assert list_resp.status_code == 200
        listed = next(d for d in list_resp.json() if d["id"] == body["id"])
        assert listed["location"] == "Empfang EG"

    async def test_register_device_without_location_is_none(self, client, db, tenant) -> None:
        resp = await client.post(
            "/api/v1/admin/devices",
            json={"name": "Ohne Standort"},
            headers=await _admin_headers(db, tenant.id),
        )
        assert resp.status_code == 201
        assert resp.json()["location"] is None


class TestDeviceTrustFix:
    """The recorded device on a device-authed punch must be the AUTHENTICATED
    device (from X-Device-Api-Key), never a client-supplied body.device_id."""

    async def test_nfc_auto_punch_records_authenticated_device_not_body_value(
        self, client, db, tenant, employee
    ) -> None:
        device, plaintext = await _create_device(db, tenant.id, name="Echtes Terminal")
        await db.commit()

        bogus_device_id = str(uuid.uuid4())
        resp = await client.post(
            "/api/v1/clock/nfc/auto",
            json={"badge_id": employee.nfc_badge_id, "device_id": bogus_device_id},
            headers={"X-Device-Api-Key": plaintext},
        )
        assert resp.status_code == 200

        from sqlalchemy import select

        from zeiterfassung.models.clock import PunchEvent

        result = await db.execute(select(PunchEvent).where(PunchEvent.employee_id == employee.id))
        event = result.scalars().one()
        assert event.device_id == str(device.id)
        assert event.device_id != bogus_device_id

    async def test_nfc_punch_records_authenticated_device_not_body_value(
        self, client, db, tenant, employee
    ) -> None:
        device, plaintext = await _create_device(db, tenant.id, name="Echtes Terminal 2")
        await db.commit()

        bogus_device_id = str(uuid.uuid4())
        resp = await client.post(
            "/api/v1/clock/nfc",
            json={
                "badge_id": employee.nfc_badge_id,
                "device_id": bogus_device_id,
                "event_type": "PUNCH_IN",
            },
            headers={"X-Device-Api-Key": plaintext},
        )
        assert resp.status_code == 201

        from sqlalchemy import select

        from zeiterfassung.models.clock import PunchEvent

        result = await db.execute(select(PunchEvent).where(PunchEvent.employee_id == employee.id))
        event = result.scalars().one()
        assert event.device_id == str(device.id)
        assert event.device_id != bogus_device_id


class TestDayDetailSurfacesDeviceLocation:
    async def test_located_device_punch_surfaces_location_in_day_detail(
        self, db, tenant, employee
    ) -> None:
        from zeiterfassung.models.clock import EventSource, EventType
        from zeiterfassung.services.clock_service import get_day_events, record_punch

        device, _plaintext = await _create_device(db, tenant.id, name="Terminal Lager")
        device.location = "Lager / Tor 3"
        await db.flush()

        event = await record_punch(
            db=db,
            tenant_id=tenant.id,
            employee_id=employee.id,
            event_type=EventType.PUNCH_IN,
            source=EventSource.NFC_TERMINAL,
            device_id=str(device.id),
        )
        await db.commit()

        events = await get_day_events(db, tenant.id, employee.id, event.event_time.date())
        assert len(events) == 1
        assert events[0]["device_name"] == "Terminal Lager"
        assert events[0]["device_location"] == "Lager / Tor 3"

    async def test_punch_without_device_has_no_location(self, db, tenant, employee) -> None:
        from zeiterfassung.models.clock import EventSource, EventType
        from zeiterfassung.services.clock_service import get_day_events, record_punch

        event = await record_punch(
            db=db,
            tenant_id=tenant.id,
            employee_id=employee.id,
            event_type=EventType.PUNCH_IN,
            source=EventSource.WEB_PORTAL,
        )
        await db.commit()

        events = await get_day_events(db, tenant.id, employee.id, event.event_time.date())
        assert len(events) == 1
        assert events[0]["device_name"] is None
        assert events[0]["device_location"] is None


# ---------------------------------------------------------------------------
# NFC endpoint hardening: rate limiting, badge_id length bound, scanned-cards
# pool cap. See CLAUDE.md task / .claude/rules/ui-limits-serverseitig-spiegeln.md
# (server-side mirroring) — these are backend-only guards, no UI counterpart.
# ---------------------------------------------------------------------------


class TestNfcRateLimiting:
    """In-process sliding-window rate limit on /nfc/* (deps.py + clock.py).

    Each test uses a freshly created device (random UUID), so the module-
    level rate-limit bucket key (device_id + client-ip) never collides across
    tests — no explicit bucket reset needed between tests.
    """

    async def test_general_rate_limit_returns_429_after_threshold(
        self, client, db, tenant, employee, monkeypatch
    ) -> None:
        monkeypatch.setenv("ZEITERFASSUNG_NFC_RATE_LIMIT_MAX_REQUESTS", "3")
        monkeypatch.setenv("ZEITERFASSUNG_NFC_RATE_LIMIT_WINDOW_SECONDS", "60")
        get_settings.cache_clear()
        try:
            _device, plaintext = await _create_device(db, tenant.id)
            await db.commit()
            headers = {"X-Device-Api-Key": plaintext}

            for _ in range(3):
                r = await client.post(
                    "/api/v1/clock/nfc/preview",
                    json={"badge_id": employee.nfc_badge_id},
                    headers=headers,
                )
                assert r.status_code == 200, r.text

            r = await client.post(
                "/api/v1/clock/nfc/preview",
                json={"badge_id": employee.nfc_badge_id},
                headers=headers,
            )
            assert r.status_code == 429
            assert "Retry-After" in r.headers
        finally:
            get_settings.cache_clear()

    async def test_failure_rate_limit_is_tighter_than_general(
        self, client, db, tenant, monkeypatch
    ) -> None:
        """Badge-id-enumeration/is_admin-oracle guard: 404-producing calls hit
        a tighter limit than the general per-device throttle."""
        monkeypatch.setenv("ZEITERFASSUNG_NFC_RATE_LIMIT_MAX_REQUESTS", "100")
        monkeypatch.setenv("ZEITERFASSUNG_NFC_RATE_LIMIT_MAX_FAILURES", "2")
        monkeypatch.setenv("ZEITERFASSUNG_NFC_RATE_LIMIT_WINDOW_SECONDS", "60")
        get_settings.cache_clear()
        try:
            _device, plaintext = await _create_device(db, tenant.id)
            await db.commit()
            headers = {"X-Device-Api-Key": plaintext}

            for _ in range(2):
                r = await client.post(
                    "/api/v1/clock/nfc/preview",
                    json={"badge_id": "TOTALLY-UNKNOWN"},
                    headers=headers,
                )
                assert r.status_code == 404

            r = await client.post(
                "/api/v1/clock/nfc/preview",
                json={"badge_id": "TOTALLY-UNKNOWN"},
                headers=headers,
            )
            assert r.status_code == 429
        finally:
            get_settings.cache_clear()

    async def test_rate_limit_scoped_per_device_not_global(
        self, client, db, tenant, employee, monkeypatch
    ) -> None:
        """A second device on the same tenant must have its own bucket."""
        monkeypatch.setenv("ZEITERFASSUNG_NFC_RATE_LIMIT_MAX_REQUESTS", "1")
        monkeypatch.setenv("ZEITERFASSUNG_NFC_RATE_LIMIT_WINDOW_SECONDS", "60")
        get_settings.cache_clear()
        try:
            _device_a, plaintext_a = await _create_device(db, tenant.id, name="A")
            _device_b, plaintext_b = await _create_device(db, tenant.id, name="B")
            await db.commit()

            r_a1 = await client.post(
                "/api/v1/clock/nfc/preview",
                json={"badge_id": employee.nfc_badge_id},
                headers={"X-Device-Api-Key": plaintext_a},
            )
            assert r_a1.status_code == 200, r_a1.text

            # Device A's bucket is now exhausted (max=1) — device B is unaffected.
            r_b1 = await client.post(
                "/api/v1/clock/nfc/preview",
                json={"badge_id": employee.nfc_badge_id},
                headers={"X-Device-Api-Key": plaintext_b},
            )
            assert r_b1.status_code == 200, r_b1.text

            r_a2 = await client.post(
                "/api/v1/clock/nfc/preview",
                json={"badge_id": employee.nfc_badge_id},
                headers={"X-Device-Api-Key": plaintext_a},
            )
            assert r_a2.status_code == 429
        finally:
            get_settings.cache_clear()


class TestBadgeIdLengthBound:
    """NFCPunchRequest.badge_id is now Field(min_length=1, max_length=64)."""

    async def test_overlong_badge_id_rejected(self, client, db, tenant) -> None:
        _device, plaintext = await _create_device(db, tenant.id)
        await db.commit()

        r = await client.post(
            "/api/v1/clock/nfc/preview",
            json={"badge_id": "A" * 65},
            headers={"X-Device-Api-Key": plaintext},
        )
        assert r.status_code == 422

    async def test_empty_badge_id_rejected(self, client, db, tenant) -> None:
        _device, plaintext = await _create_device(db, tenant.id)
        await db.commit()

        r = await client.post(
            "/api/v1/clock/nfc/preview",
            json={"badge_id": ""},
            headers={"X-Device-Api-Key": plaintext},
        )
        assert r.status_code == 422

    async def test_max_length_badge_id_accepted_at_boundary(self, client, db, tenant) -> None:
        _device, plaintext = await _create_device(db, tenant.id)
        await db.commit()

        r = await client.post(
            "/api/v1/clock/nfc/preview",
            json={"badge_id": "A" * 64},
            headers={"X-Device-Api-Key": plaintext},
        )
        # Passes schema validation, resolves normally to "unknown badge" (404).
        assert r.status_code == 404


class TestScannedCardsPoolCap:
    """``_remember_unknown_card`` caps ``tenant.settings["scanned_cards"]`` at
    ``settings.nfc_scanned_cards_max`` (oldest evicted first)."""

    async def test_unknown_card_pool_evicts_oldest_when_over_cap(
        self, client, db, tenant, monkeypatch
    ) -> None:
        monkeypatch.setenv("ZEITERFASSUNG_NFC_SCANNED_CARDS_MAX", "3")
        get_settings.cache_clear()
        try:
            _device, plaintext = await _create_device(db, tenant.id)
            await db.commit()
            headers = {"X-Device-Api-Key": plaintext}

            for i in range(5):
                r = await client.post(
                    "/api/v1/clock/nfc/preview",
                    json={"badge_id": f"UNKNOWN-{i:03d}"},
                    headers=headers,
                )
                assert r.status_code == 404, r.text

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
            uids = [c["uid"] for c in r2.json()]
            assert len(uids) == 3
            # Oldest (UNKNOWN-000, -001) evicted; last 3 remain, in order.
            assert uids == ["UNKNOWN-002", "UNKNOWN-003", "UNKNOWN-004"]
        finally:
            get_settings.cache_clear()
