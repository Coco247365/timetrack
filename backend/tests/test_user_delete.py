"""DELETE /users/{id}: endgueltiges Loeschen soft-geloeschter Mitarbeiter (Issue #156)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from zeiterfassung.core.security import create_access_token, hash_password
from zeiterfassung.models.account import Account
from zeiterfassung.models.clock import EventType, PunchEvent
from zeiterfassung.models.user import User


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


async def _user(db, tenant, *, status="deleted", number="D001"):
    now = datetime.now(tz=UTC)
    u = User(
        tenant_id=tenant.id,
        email=f"{number.lower()}@t.de",
        hashed_password=hash_password("Passw0rd!"),
        first_name="Falsch",
        last_name="Angelegt",
        employee_number=number,
        contracted_hours_per_week=40.0,
        is_active=status == "active",
        employment_status=status,
        created_at=now,
        updated_at=now,
    )
    db.add(u)
    await db.commit()
    return u


async def _actor(db, tenant, email="actor@t.de"):
    """Echtes Login-Konto — get_current_user prüft seit der Login-Härtung
    (#161) Existenz/is_active/token_version des Kontos pro Request."""
    now = datetime.now(tz=UTC)
    acc = Account(
        tenant_id=tenant.id,
        email=email,
        hashed_password=hash_password("Passw0rd!"),
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    db.add(acc)
    await db.commit()
    return acc


def _headers(tenant_id, actor, *, perms=("users:write", "users:read_all"), superadmin=False):
    tok = create_access_token(
        user_id=str(actor.id),
        tenant_id=str(tenant_id),
        permissions=list(perms),
        department_id=None,
        role_type="geschaeftsfuehrung",
        data_scope="global",
        is_superadmin=superadmin,
        token_version=actor.token_version or 0,
    )
    return {"Authorization": f"Bearer {tok}"}


class TestUserDelete:
    async def test_geloeschten_ohne_daten_entfernen(self, db, client, tenant):
        u = await _user(db, tenant)
        # Verknuepftes Konto: darf nur die Verknuepfung verlieren.
        now = datetime.now(tz=UTC)
        acc = Account(
            tenant_id=tenant.id,
            email="login@t.de",
            hashed_password=hash_password("Passw0rd!"),
            is_active=False,
            employee_id=u.id,
            created_at=now,
            updated_at=now,
        )
        db.add(acc)
        await db.commit()

        uid, acc_id = u.id, acc.id
        actor = await _actor(db, tenant)
        r = await client.delete(f"/api/v1/users/{uid}", headers=_headers(tenant.id, actor))
        assert r.status_code == 204, r.text

        db.expunge_all()  # App schrieb ueber eine eigene Session
        assert await db.get(User, uid) is None
        acc2 = await db.get(Account, acc_id)
        assert acc2 is not None  # Konto bleibt
        assert acc2.employee_id is None  # nur die Verknuepfung ist weg

    async def test_mit_stempelung_409(self, db, client, tenant):
        u = await _user(db, tenant, number="D002")
        db.add(
            PunchEvent(
                tenant_id=tenant.id,
                employee_id=u.id,
                event_type=EventType.PUNCH_IN,
                event_time=datetime.now(tz=UTC),
                source="NFC_TERMINAL",
                created_at=datetime.now(tz=UTC),
            )
        )
        await db.commit()
        actor = await _actor(db, tenant)
        r = await client.delete(f"/api/v1/users/{u.id}", headers=_headers(tenant.id, actor))
        assert r.status_code == 409, r.text
        assert "archivieren" in r.json()["detail"]
        assert await db.get(User, u.id) is not None

    async def test_aktiver_mitarbeiter_409(self, db, client, tenant):
        u = await _user(db, tenant, status="active", number="D003")
        actor = await _actor(db, tenant)
        r = await client.delete(f"/api/v1/users/{u.id}", headers=_headers(tenant.id, actor))
        assert r.status_code == 409, r.text

    async def test_archivierter_mitarbeiter_409(self, db, client, tenant):
        u = await _user(db, tenant, status="archived", number="D004")
        actor = await _actor(db, tenant)
        r = await client.delete(f"/api/v1/users/{u.id}", headers=_headers(tenant.id, actor))
        assert r.status_code == 409, r.text

    async def test_ohne_recht_403(self, db, client, tenant):
        u = await _user(db, tenant, number="D005")
        actor = await _actor(db, tenant)
        r = await client.delete(
            f"/api/v1/users/{u.id}", headers=_headers(tenant.id, actor, perms=("users:read_all",))
        )
        assert r.status_code == 403, r.text

    async def test_fremder_tenant_404(self, db, client, tenant):
        actor = await _actor(db, tenant)
        r = await client.delete(f"/api/v1/users/{uuid.uuid4()}", headers=_headers(tenant.id, actor))
        assert r.status_code == 404, r.text
