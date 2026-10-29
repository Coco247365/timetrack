"""Profil-Regel wochenende_ins_konto: WE-/Feiertagsarbeit ins Ueberstundenkonto.

Beleg: echte Beta-LODAS 06/2026, PN 42 (Auszahlung inkl. Samstag).
Default AUS = Bestandsverhalten (#21: WE/Feiertag hat eigene Zuschlags-Lohnart).

Die Ledger-Berechnung (``overtime_service._daily_rows_for_month`` u.a.) liest
Tagesboegen direkt aus ``DailyTimesheet`` (siehe tests/test_overtime_daily.py) —
darum wird hier, wie in den bestehenden Ueberstunden-Tests, ein Tagesbogen
direkt angelegt statt Punch-Events zu stempeln.
"""

from __future__ import annotations

import uuid as _uuid
from datetime import UTC, date, datetime

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from zeiterfassung.core.security import create_access_token
from zeiterfassung.models.clock import DailyTimesheet
from zeiterfassung.services import overtime_service

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _ts(tenant_id, emp_id, d: date, net: int) -> DailyTimesheet:
    return DailyTimesheet(
        tenant_id=tenant_id,
        employee_id=emp_id,
        work_date=d,
        start_time=datetime(d.year, d.month, d.day, 8, 0, tzinfo=UTC),
        break_minutes=0,
        net_work_minutes=net,
        gross_work_minutes=net,
        overtime_minutes=0,
        is_holiday_work=False,
        created_at=NOW,
        updated_at=NOW,
    )


def _profil(tenant, *, flag: bool) -> None:
    tenant.settings = {
        **(tenant.settings or {}),
        "wage_profiles": [
            {"id": "p1", "name": "Beta", "mapping": {}, "wochenende_ins_konto": flag}
        ],
    }


class TestWochenendeInsKonto:
    async def test_samstag_zaehlt_mit_flag(self, db, tenant, employee):
        # Samstag 06.06.2026, 4h Arbeit; Flag AN -> Samstags-Minuten zaehlen mit.
        _profil(tenant, flag=True)
        employee.wage_profile_id = "p1"
        db.add(_ts(tenant.id, employee.id, date(2026, 6, 6), 240))
        await db.commit()

        net_an = await overtime_service._net_overtime_minutes_for_month(
            db, tenant.id, employee, 2026, 6, today=date(2026, 6, 8)
        )

        _profil(tenant, flag=False)
        await db.commit()

        net_aus = await overtime_service._net_overtime_minutes_for_month(
            db, tenant.id, employee, 2026, 6, today=date(2026, 6, 8)
        )

        # Entscheidend: Differenz zwischen Flag an/aus == die Samstags-Minuten.
        assert net_an - net_aus == 240

    async def test_samstag_zaehlt_nicht_ohne_flag(self, db, tenant, employee):
        _profil(tenant, flag=False)
        employee.wage_profile_id = "p1"
        db.add(_ts(tenant.id, employee.id, date(2026, 6, 6), 240))
        await db.commit()

        net = await overtime_service._net_overtime_minutes_for_month(
            db, tenant.id, employee, 2026, 6, today=date(2026, 6, 8)
        )
        # Samstag traegt nichts bei (0 statt +240); Werktage ohne Stempel duerfen
        # als unentschuldigtes Fehlen negativ einfliessen (Bestandsverhalten).
        assert net <= 0


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


async def _admin_headers(db: AsyncSession, tenant_id: _uuid.UUID) -> dict[str, str]:
    from tests.conftest import ensure_login_account

    acc = await ensure_login_account(db, tenant_id)
    tok = create_access_token(
        user_id=str(acc.id),
        tenant_id=str(tenant_id),
        permissions=["admin:settings"],
        department_id=None,
        role_type="geschaeftsfuehrung",
        data_scope="global",
    )
    return {"Authorization": f"Bearer {tok}"}


class TestWochenendeInsKontoApi:
    async def test_api_speichert_flag(self, db, client, tenant):
        hdr = await _admin_headers(db, tenant.id)
        r = await client.patch(
            "/api/v1/admin/wage-profiles",
            headers=hdr,
            json={"name": "T", "mapping": {}, "wochenende_ins_konto": True},
        )
        assert r.status_code == 200, r.text
        profs = (await client.get("/api/v1/admin/wage-profiles", headers=hdr)).json()["profiles"]
        assert any(p.get("wochenende_ins_konto") is True for p in profs)

    async def test_api_standard_422(self, db, client, tenant):
        hdr = await _admin_headers(db, tenant.id)
        r = await client.patch(
            "/api/v1/admin/wage-profiles",
            headers=hdr,
            json={
                "id": "standard",
                "name": "Standard",
                "mapping": {},
                "wochenende_ins_konto": True,
            },
        )
        assert r.status_code == 422, r.text


class TestExportStaffelWochenende:
    """Export Soll-Modus, payout_mode=all (#Beleg PN5 Mai 2026): WE-Ist zaehlt
    nur mit Profil-Regel in die Mehrarbeit-Staffel — sonst laeuft der Samstag
    allein ueber die Zuschlags-Lohnart (015), nicht in 090/062/066."""

    WAGE_TYPES = {
        "NORMALE_ARBEITSZEIT": 11,
        "MEHRARBEIT_0": 62,
        "MEHRARBEIT_ZUSCHLAG": 66,
        "MINUSSTUNDEN": 13,
    }

    async def _setup(self, db, tenant, employee, flag: bool) -> None:
        employee.overtime_payout_mode = "all"
        employee.time_capture = "auto"
        employee.wage_profile_id = "p1"
        tenant.settings = {
            **(tenant.settings or {}),
            "zuschlaege": {"export_basis": "soll", "mehrarbeit_frei_wochen_std": 1.5},
            "wage_profiles": [{"id": "p1", "name": "P", "wochenende_ins_konto": flag}],
        }
        # Samstag 06.06.2026: 4h Arbeit (08:00-12:00 UTC, keine Pausen).
        from zeiterfassung.models.clock import EventSource, EventType, PunchEvent

        for etype, hh in ((EventType.PUNCH_IN, 8), (EventType.PUNCH_OUT, 12)):
            db.add(
                PunchEvent(
                    tenant_id=tenant.id,
                    employee_id=employee.id,
                    event_type=etype,
                    event_time=datetime(2026, 6, 6, hh, 0, tzinfo=UTC),
                    source=EventSource.NFC_TERMINAL,
                    created_at=NOW,
                )
            )
        await db.commit()

    async def _rows(self, db, tenant, employee) -> dict[int, float]:
        from zeiterfassung.services.export_service import _build_datev_rows

        rows = await _build_datev_rows(db, tenant.id, 2026, 6, [employee.id], self.WAGE_TYPES)
        out: dict[int, float] = {}
        for r in rows:
            out[r.lohnart] = round(out.get(r.lohnart, 0.0) + r.wert, 2)
        return out

    async def test_flag_aus_samstag_nicht_in_staffel(self, db, tenant, employee):
        await self._setup(db, tenant, employee, flag=False)
        by = await self._rows(db, tenant, employee)
        assert 62 not in by and 66 not in by

    async def test_flag_an_samstag_in_staffel(self, db, tenant, employee):
        await self._setup(db, tenant, employee, flag=True)
        by = await self._rows(db, tenant, employee)
        # 4h Samstag: 1,5h frei (Wochenkontingent) + 2,5h mit Zuschlag.
        assert by.get(62) == 1.5 and by.get(66) == 2.5
