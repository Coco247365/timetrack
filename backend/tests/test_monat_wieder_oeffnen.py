"""Versehentlichen Abschluss des laufenden Monats zuruecknehmen (reopen).

Anlass 24.08.2026: "Monat neu abschliessen" auf der Export-Seite wurde fuer den
laufenden August gedrueckt — 38 Mitarbeiter mitten im Monat eingefroren, 13
vorzeitige Auszahlungen gebucht. Der Export-Endpunkt hatte (anders als der
Einstellungen-Endpunkt) keinen Schutz, und es gab keinen Weg zurueck: resettle
kann nur loeschen UND sofort neu abschliessen.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from zeiterfassung.core.security import create_access_token, hash_password
from zeiterfassung.models.overtime import OvertimeEntry
from zeiterfassung.models.user import User
from zeiterfassung.services import overtime_service

HEUTE = datetime.now(tz=UTC).date()


async def _mitarbeiter(db, tenant) -> User:
    now = datetime.now(tz=UTC)
    u = User(
        tenant_id=tenant.id,
        email="reopen@test.de",
        hashed_password=hash_password("Passw0rd!"),
        first_name="Rita",
        last_name="Reopen",
        employee_number="RO-1",
        contracted_hours_per_week=40.0,
        # Tages-Soll 0: die Ansammlung des angebrochenen Monats ist damit 0 und
        # der Abschluss zahlt deterministisch (Saldo - Kappe) aus.
        weekday_target_minutes=[0, 0, 0, 0, 0, 0, 0],
        employment_type="hourly",
        overtime_payout_mode="above_cap",
        overtime_cap_hours=20.0,
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    db.add(u)
    await db.flush()
    return u


async def _eintraege(db, tenant, user) -> dict[str, list[float]]:
    rows = (
        (
            await db.execute(
                select(OvertimeEntry).where(
                    OvertimeEntry.tenant_id == tenant.id,
                    OvertimeEntry.employee_id == user.id,
                )
            )
        )
        .scalars()
        .all()
    )
    aus: dict[str, list[float]] = {}
    for r in rows:
        aus.setdefault(r.entry_type, []).append(float(r.hours))
    return aus


class TestReopenService:
    async def test_reopen_loescht_abschluss_und_laesst_offen(self, db, tenant) -> None:
        u = await _mitarbeiter(db, tenant)
        # Startsaldo 30 h ueber Kappe 20 -> der (verfruehte) Abschluss zahlt 10 aus.
        await overtime_service.add_adjustment(
            db, tenant.id, u.id, 30, "Anfangsbestand", date(HEUTE.year, HEUTE.month, 1), None
        )
        await overtime_service.settle_month(db, tenant.id, u, HEUTE.year, HEUTE.month)
        vorher = await _eintraege(db, tenant, u)
        assert "accrual" in vorher and "payout" in vorher  # eingefroren

        await overtime_service.reopen_month(db, tenant.id, u, HEUTE.year, HEUTE.month)
        nachher = await _eintraege(db, tenant, u)
        assert "accrual" not in nachher and "payout" not in nachher
        assert nachher.get("adjustment") == [30.0]  # Korrekturen bleiben

    async def test_reopen_ist_idempotent(self, db, tenant) -> None:
        u = await _mitarbeiter(db, tenant)
        r = await overtime_service.reopen_month(db, tenant.id, u, HEUTE.year, HEUTE.month)
        assert float(r["alte_auszahlung"]) == 0.0


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


async def _headers(db: AsyncSession, tenant_id: uuid.UUID) -> dict[str, str]:
    from tests.conftest import ensure_login_account

    acc = await ensure_login_account(db, tenant_id)
    tok = create_access_token(
        user_id=str(acc.id),
        tenant_id=str(tenant_id),
        permissions=["export:datev"],
        department_id=None,
        role_type="geschaeftsfuehrung",
        data_scope="global",
    )
    return {"Authorization": f"Bearer {tok}"}


class TestGuards:
    async def test_resettle_lehnt_laufenden_monat_ab(self, db, tenant, client) -> None:
        h = await _headers(db, tenant.id)
        await db.commit()
        r = await client.post(
            "/api/v1/export/datev/resettle",
            json={"year": HEUTE.year, "month": HEUTE.month},
            headers=h,
        )
        assert r.status_code == 422
        assert "laufende Monat" in r.json()["detail"]

    async def test_reopen_lehnt_vergangenen_monat_ab(self, db, tenant, client) -> None:
        h = await _headers(db, tenant.id)
        await db.commit()
        vor = date(HEUTE.year - 1, 12, 1)
        r = await client.post(
            "/api/v1/export/datev/reopen",
            json={"year": vor.year, "month": vor.month},
            headers=h,
        )
        assert r.status_code == 422
        assert "Vergangene Monate" in r.json()["detail"]

    async def test_reopen_laufender_monat_ok(self, db, tenant, client) -> None:
        h = await _headers(db, tenant.id)
        await db.commit()
        r = await client.post(
            "/api/v1/export/datev/reopen",
            json={"year": HEUTE.year, "month": HEUTE.month},
            headers=h,
        )
        assert r.status_code == 200
        assert "reopened" in r.json()


class TestFehlermeldung:
    """Die Sperre spricht dieselbe Sprache wie die Oberflaeche ("neu berechnen").

    Der Nutzer sieht den Serversatz 1:1 im roten Kasten — er darf nicht von
    "Monatsabschluss" reden, wenn der Knopf "Überstunden neu berechnen" heisst.
    """

    async def test_export_pfad_nennt_neu_berechnen(self, db, tenant, client) -> None:
        h = await _headers(db, tenant.id)
        await db.commit()
        r = await client.post(
            "/api/v1/export/datev/resettle",
            json={"year": HEUTE.year, "month": HEUTE.month},
            headers=h,
        )
        assert r.status_code == 422
        assert "neu berechnen" in r.json()["detail"]
        assert "abschlie" not in r.json()["detail"].lower()

    async def test_einstellungen_pfad_nennt_neu_berechnen(self, db, tenant, client) -> None:
        from tests.conftest import ensure_login_account

        acc = await ensure_login_account(db, tenant.id)
        tok = create_access_token(
            user_id=str(acc.id),
            tenant_id=str(tenant.id),
            permissions=["overtime:manage"],
            department_id=None,
            role_type="geschaeftsfuehrung",
            data_scope="global",
        )
        await db.commit()
        r = await client.post(
            "/api/v1/admin/overtime/resettle",
            json={"year": HEUTE.year, "month": HEUTE.month, "employee_id": None},
            headers={"Authorization": f"Bearer {tok}"},
        )
        assert r.status_code == 422
        assert "neu berechnen" in r.json()["detail"]
