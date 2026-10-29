"""Pool-Konsistenz bei Badge-Zuweisung (bead qei) + Handy-UID-Anzeige (bead bpa).

Eine Karte darf nicht gleichzeitig einem Mitarbeiter zugewiesen UND im
scanned_cards-Pool als "frei" stehen. Beim Zuweisen wird der Pool-Eintrag
entfernt (Metadaten wie chip_uid bleiben unter assigned_card_meta erhalten);
beim Entzug/Umhaengen/Archivieren kommt die Karte mitsamt Metadaten zurueck
in den Pool. Manuell vergebene Alt-Badges, die nie im Pool waren, landen
beim Freiwerden NICHT im Pool.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from zeiterfassung.models.tenant import Tenant
from zeiterfassung.models.user import User
from zeiterfassung.schemas.user import UserUpdate
from zeiterfassung.services.user_service import update_user

TOKEN = "7fb1a557deadbeef"
CHIP_UID = "04a1b2c3d4e5f6"


async def _pool_card(db: AsyncSession, tenant: Tenant, uid: str = TOKEN) -> None:
    settings = dict(tenant.settings or {})
    cards = list(settings.get("scanned_cards") or [])
    cards.append({"uid": uid, "chip_uid": CHIP_UID, "at": datetime.now(UTC).isoformat()})
    settings["scanned_cards"] = cards
    tenant.settings = settings
    await db.flush()


async def _fresh_settings(db: AsyncSession, tenant: Tenant) -> dict:
    return (
        await db.execute(select(Tenant.settings).where(Tenant.id == tenant.id))
    ).scalar_one() or {}


def _pool_uids(settings: dict) -> set[str]:
    return {c.get("uid") for c in (settings.get("scanned_cards") or [])}


class TestZuweisungEntferntAusPool:
    async def test_zuweisung_entfernt_karte_aus_pool(self, db, tenant, employee):
        await _pool_card(db, tenant)

        await update_user(db, tenant.id, employee.id, UserUpdate(nfc_badge_id=TOKEN))

        settings = await _fresh_settings(db, tenant)
        assert TOKEN not in _pool_uids(settings)
        # Metadaten (chip_uid) bleiben fuer spaeteres Freiwerden erhalten
        assert settings["assigned_card_meta"][TOKEN]["chip_uid"] == CHIP_UID
        assert employee.nfc_badge_id == TOKEN

    async def test_entzug_bringt_karte_mit_chip_uid_zurueck(self, db, tenant, employee):
        await _pool_card(db, tenant)
        await update_user(db, tenant.id, employee.id, UserUpdate(nfc_badge_id=TOKEN))

        await update_user(db, tenant.id, employee.id, UserUpdate(nfc_badge_id=None))

        settings = await _fresh_settings(db, tenant)
        entry = next(c for c in settings["scanned_cards"] if c["uid"] == TOKEN)
        assert entry["chip_uid"] == CHIP_UID
        assert TOKEN not in (settings.get("assigned_card_meta") or {})

    async def test_umhaengen_gibt_alte_frei_und_nimmt_neue(self, db, tenant, employee):
        other = "aa11bb22cc33dd44"
        await _pool_card(db, tenant, uid=TOKEN)
        await _pool_card(db, tenant, uid=other)
        await update_user(db, tenant.id, employee.id, UserUpdate(nfc_badge_id=TOKEN))

        await update_user(db, tenant.id, employee.id, UserUpdate(nfc_badge_id=other))

        settings = await _fresh_settings(db, tenant)
        pool = _pool_uids(settings)
        assert TOKEN in pool  # alte Karte wieder frei
        assert other not in pool  # neue Karte nicht mehr frei

    async def test_alt_badge_ohne_pool_herkunft_landet_nicht_im_pool(self, db, tenant, employee):
        # Fixture-Badge BADGE001 war nie im Pool -> Entzug pollt ihn nicht rein
        assert employee.nfc_badge_id == "BADGE001"

        await update_user(db, tenant.id, employee.id, UserUpdate(nfc_badge_id=None))

        settings = await _fresh_settings(db, tenant)
        assert "BADGE001" not in _pool_uids(settings)

    async def test_archivierung_gibt_pool_karte_zurueck(self, db, tenant, employee):
        await _pool_card(db, tenant)
        await update_user(db, tenant.id, employee.id, UserUpdate(nfc_badge_id=TOKEN))

        yesterday = date.today() - timedelta(days=1)
        await update_user(db, tenant.id, employee.id, UserUpdate(termination_date=yesterday))

        emp = (await db.execute(select(User).where(User.id == employee.id))).scalar_one()
        assert emp.employment_status == "archived"
        assert emp.nfc_badge_id is None
        settings = await _fresh_settings(db, tenant)
        entry = next(c for c in settings["scanned_cards"] if c["uid"] == TOKEN)
        assert entry["chip_uid"] == CHIP_UID

    async def test_admin_employees_liefert_chip_uid_zugewiesener_karte(self, db, tenant, employee):
        # bpa: Nach der Zuweisung liegt die Handy-UID in assigned_card_meta —
        # /admin/employees muss sie als nfc_chip_uid ausliefern, sonst zeigt
        # die WebApp fuer zugewiesene Karten keine UID mehr.
        await _pool_card(db, tenant)
        await update_user(db, tenant.id, employee.id, UserUpdate(nfc_badge_id=TOKEN))

        emps = await self._fetch_admin_employees(db, tenant)
        me = next(e for e in emps if e["id"] == str(employee.id))
        assert me["nfc_badge_id"] == TOKEN
        assert me["nfc_chip_uid"] == CHIP_UID

    async def test_admin_employees_chip_uid_fallback_altbestand(self, db, tenant, employee):
        # Vor dem Pool-Konsistenz-Fix zugewiesene Karten stehen noch im Pool
        # (kein assigned_card_meta) — der Fallback liest chip_uid von dort.
        await _pool_card(db, tenant)
        employee.nfc_badge_id = TOKEN  # Direktzuweisung wie vor dem Fix
        await db.flush()

        emps = await self._fetch_admin_employees(db, tenant)
        me = next(e for e in emps if e["id"] == str(employee.id))
        assert me["nfc_chip_uid"] == CHIP_UID

    async def _fetch_admin_employees(self, db: AsyncSession, tenant: Tenant) -> list[dict]:
        from httpx import ASGITransport, AsyncClient
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from tests.conftest import ensure_login_account
        from zeiterfassung.api import deps
        from zeiterfassung.core.security import create_access_token
        from zeiterfassung.main import create_app

        factory = async_sessionmaker(db.bind, expire_on_commit=False, class_=AsyncSession)

        async def override_get_db():
            async with factory() as session:
                yield session

        acc = await ensure_login_account(db, tenant.id)
        await db.commit()  # Stand fuer die zweite Session sichtbar machen
        app = create_app()
        app.dependency_overrides[deps.get_db] = override_get_db
        token = create_access_token(
            user_id=str(acc.id),
            tenant_id=str(tenant.id),
            permissions=["users:read_all"],
        )
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="https://test"
            ) as client:
                resp = await client.get(
                    "/api/v1/admin/employees", headers={"Authorization": f"Bearer {token}"}
                )
        finally:
            app.dependency_overrides.clear()
        assert resp.status_code == 200
        return resp.json()

    async def test_bestehender_altbestand_wird_nicht_dupliziert(self, db, tenant, employee):
        # Altbestand vor dem Fix: Karte zugewiesen UND noch im Pool. Beim
        # Freiwerden darf kein zweiter Pool-Eintrag entstehen.
        await _pool_card(db, tenant)
        employee.nfc_badge_id = TOKEN  # Direktzuweisung wie vor dem Fix
        await db.flush()

        await update_user(db, tenant.id, employee.id, UserUpdate(nfc_badge_id=None))

        settings = await _fresh_settings(db, tenant)
        assert sum(1 for c in settings["scanned_cards"] if c["uid"] == TOKEN) == 1
