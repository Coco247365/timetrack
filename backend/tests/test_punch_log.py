"""GET /clock/punch-log/{employee_id}: unveraenderbares Stempel-Protokoll (Issue #167)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from zeiterfassung.core.security import create_access_token, hash_password
from zeiterfassung.models.account import Account
from zeiterfassung.models.clock import EventSource, EventType, PunchEvent
from zeiterfassung.models.device import Device


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


async def _actor(db, tenant, email="log-actor@t.de"):
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


def _headers(tenant_id, actor, *, perms=(), employee_id=None):
    tok = create_access_token(
        user_id=str(actor.id),
        tenant_id=str(tenant_id),
        permissions=list(perms),
        department_id=None,
        role_type="mitarbeiter" if not perms else "geschaeftsfuehrung",
        data_scope="self" if not perms else "global",
        employee_id=str(employee_id) if employee_id else None,
        token_version=actor.token_version or 0,
    )
    return {"Authorization": f"Bearer {tok}"}


def _punch(tenant, employee_id, *, hour, **kw):
    now = datetime(2026, 7, 15, hour, 0, tzinfo=UTC)
    defaults = {
        "event_type": EventType.PUNCH_IN,
        "event_time": now,
        "source": EventSource.NFC_TERMINAL,
        "created_at": now,
    }
    defaults.update(kw)
    return PunchEvent(tenant_id=tenant.id, employee_id=employee_id, **defaults)


class TestPunchLog:
    async def test_korrektur_kette_und_effective(self, db, client, tenant, employee):
        orig = _punch(tenant, employee.id, hour=8)
        db.add(orig)
        await db.commit()
        korr = _punch(
            tenant,
            employee.id,
            hour=7,
            source=EventSource.ADMIN_MANUAL,
            corrects_event_id=orig.id,
            reason="Terminal defekt",
        )
        db.add(korr)
        await db.commit()

        actor = await _actor(db, tenant)
        r = await client.get(
            f"/api/v1/clock/punch-log/{employee.id}?date_from=2026-07-15&date_to=2026-07-15",
            headers=_headers(tenant.id, actor, perms=("timesheet:view_all",)),
        )
        assert r.status_code == 200, r.text
        rows = {e["id"]: e for e in r.json()}
        assert rows[orig.id]["effective"] is False  # ersetzt
        assert rows[korr.id]["effective"] is True
        assert rows[korr.id]["corrects_event_id"] == orig.id
        assert rows[korr.id]["reason"] == "Terminal defekt"
        assert rows[orig.id]["source"] == "NFC_TERMINAL"

    async def test_void_bleibt_sichtbar_aber_unwirksam(self, db, client, tenant, employee):
        ev = _punch(tenant, employee.id, hour=9)
        db.add(ev)
        await db.commit()
        void = _punch(
            tenant,
            employee.id,
            hour=9,
            source=EventSource.ADMIN_MANUAL,
            corrects_event_id=ev.id,
            is_void=True,
            reason="Versehentlicher Doppel-Tap",
        )
        db.add(void)
        await db.commit()

        actor = await _actor(db, tenant)
        r = await client.get(
            f"/api/v1/clock/punch-log/{employee.id}?date_from=2026-07-15&date_to=2026-07-15",
            headers=_headers(tenant.id, actor, perms=("timesheet:view_all",)),
        )
        rows = {e["id"]: e for e in r.json()}
        assert rows[ev.id]["effective"] is False
        assert rows[void.id]["is_void"] is True
        assert rows[void.id]["effective"] is False  # Void-Marker selbst zaehlt nie

    async def test_eigene_sicht_erlaubt_fremde_403(self, db, client, tenant, employee):
        db.add(_punch(tenant, employee.id, hour=8))
        await db.commit()
        actor = await _actor(db, tenant, email="self@t.de")

        eigene = await client.get(
            f"/api/v1/clock/punch-log/{employee.id}?date_from=2026-07-15&date_to=2026-07-15",
            headers=_headers(tenant.id, actor, employee_id=employee.id),
        )
        assert eigene.status_code == 200, eigene.text
        assert len(eigene.json()) == 1

        fremd = await client.get(
            f"/api/v1/clock/punch-log/{uuid.uuid4()}?date_from=2026-07-15&date_to=2026-07-15",
            headers=_headers(tenant.id, actor, employee_id=employee.id),
        )
        assert fremd.status_code == 403

    async def test_korrektur_traegt_login_konto(self, db, client, tenant, employee):
        """Korrektur über die API stempelt das ausführende Login-Konto ein (#169)."""
        orig = _punch(tenant, employee.id, hour=8)
        db.add(orig)
        await db.commit()

        actor = await _actor(db, tenant, email="chef@t.de")
        h = _headers(tenant.id, actor, perms=("timesheet:view_all", "timesheet:edit_all"))
        r = await client.post(
            f"/api/v1/clock/events/{orig.id}/correct",
            json={"event_time": "2026-07-15T07:30:00Z", "reason": "Terminal defekt"},
            headers=h,
        )
        assert r.status_code == 200, r.text

        log = await client.get(
            f"/api/v1/clock/punch-log/{employee.id}?date_from=2026-07-15&date_to=2026-07-15",
            headers=h,
        )
        rows = {e["id"]: e for e in log.json()}
        korr = next(e for e in rows.values() if e["corrects_event_id"] == orig.id)
        assert korr["created_by_account_email"] == "chef@t.de"
        # Kiosk-Original hat kein Konto (und Altbestand bleibt None).
        assert rows[orig.id]["created_by_account_email"] is None

    async def test_terminal_name_statt_geraete_uuid(self, db, client, tenant, employee):
        """Die Quelle-Spalte zeigt den Terminal-Namen ("Alpha"), nicht die Device-UUID."""
        now = datetime(2026, 7, 15, 6, 0, tzinfo=UTC)
        device = Device(
            tenant_id=tenant.id,
            name="Alpha",
            location="Werkhalle",
            key_hash="a" * 64,
            is_active=True,
            created_at=now,
            updated_at=now,
        )
        db.add(device)
        await db.commit()

        vom_terminal = _punch(tenant, employee.id, hour=6, device_id=str(device.id))
        # Fremdes/geloeschtes Geraet: kein Name aufloesbar -> None (UI kuerzt die ID).
        unbekannt = _punch(tenant, employee.id, hour=7, device_id=str(uuid.uuid4()))
        manuell = _punch(tenant, employee.id, hour=8, source=EventSource.ADMIN_MANUAL)
        db.add_all([vom_terminal, unbekannt, manuell])
        await db.commit()

        actor = await _actor(db, tenant, email="quelle@t.de")
        r = await client.get(
            f"/api/v1/clock/punch-log/{employee.id}?date_from=2026-07-15&date_to=2026-07-15",
            headers=_headers(tenant.id, actor, perms=("timesheet:view_all",)),
        )
        assert r.status_code == 200, r.text
        rows = {e["id"]: e for e in r.json()}
        assert rows[vom_terminal.id]["device_name"] == "Alpha"
        assert rows[vom_terminal.id]["device_id"] == str(device.id)  # Rohwert bleibt erhalten
        assert rows[unbekannt.id]["device_name"] is None
        assert rows[manuell.id]["device_name"] is None

    async def test_terminal_name_bleibt_mandantengetrennt(self, db, client, tenant, employee):
        """Ein Geraet eines fremden Mandanten wird nie aufgeloest."""
        now = datetime(2026, 7, 15, 6, 0, tzinfo=UTC)
        fremd = Device(
            tenant_id=uuid.uuid4(),
            name="Außenstelle",
            key_hash="b" * 64,
            is_active=True,
            created_at=now,
            updated_at=now,
        )
        db.add(fremd)
        await db.commit()

        ev = _punch(tenant, employee.id, hour=6, device_id=str(fremd.id))
        db.add(ev)
        await db.commit()

        actor = await _actor(db, tenant, email="scope@t.de")
        r = await client.get(
            f"/api/v1/clock/punch-log/{employee.id}?date_from=2026-07-15&date_to=2026-07-15",
            headers=_headers(tenant.id, actor, perms=("timesheet:view_all",)),
        )
        assert r.json()[0]["device_name"] is None

    async def test_zeitraum_validierung(self, db, client, tenant, employee):
        actor = await _actor(db, tenant)
        h = _headers(tenant.id, actor, perms=("timesheet:view_all",))
        r1 = await client.get(
            f"/api/v1/clock/punch-log/{employee.id}?date_from=2026-07-15&date_to=2026-07-01",
            headers=h,
        )
        assert r1.status_code == 422
        r2 = await client.get(
            f"/api/v1/clock/punch-log/{employee.id}?date_from=2024-01-01&date_to=2026-07-15",
            headers=h,
        )
        assert r2.status_code == 422
