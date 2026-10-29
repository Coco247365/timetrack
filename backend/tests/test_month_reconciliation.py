"""Monatsabgleich je Mitarbeiter (GET /admin/month-reconciliation).

Prueft die Soll-Kette (Bruttosoll − Feiertag − Urlaub = effektives Soll),
den Ueberstundenblock aus dem Ledger, die Lohnarten-Zeilen (dieselbe
Rechnung wie der LODAS-Export) und die Berechtigungspruefung.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from zeiterfassung.core.security import create_access_token, hash_password
from zeiterfassung.models.absence import RequestType
from zeiterfassung.models.clock import EventSource, EventType, PunchEvent
from zeiterfassung.models.export import WageTypeMapping
from zeiterfassung.models.overtime import OvertimeEntry
from zeiterfassung.models.user import User
from zeiterfassung.policy.context import DayPart
from zeiterfassung.schemas.absence import AbsenceRequestCreate
from zeiterfassung.services.absence_service import create_request


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


async def _token(db: AsyncSession, tenant_id: uuid.UUID, *, all_rights: bool) -> str:
    from tests.conftest import ensure_login_account

    acc = await ensure_login_account(db, tenant_id)
    return create_access_token(
        user_id=str(acc.id),
        tenant_id=str(tenant_id),
        permissions=["timesheet:view_all", "admin:dashboard"]
        if all_rights
        else ["timesheet:view_own"],
        department_id=None,
        role_type="geschaeftsfuehrung" if all_rights else "mitarbeiter",
        data_scope="global" if all_rights else "self",
    )


@pytest_asyncio.fixture
async def plan_employee(db, tenant) -> User:
    """Mo–Do 8:15 (495), Fr 5:30 (330); Modus above_cap mit Kappe 20 h."""
    now = datetime.now(tz=UTC)
    u = User(
        tenant_id=tenant.id,
        email="abgleich@test.de",
        hashed_password=hash_password("Passw0rd!"),
        first_name="Anna",
        last_name="Abgleich",
        employee_number="77",
        contracted_hours_per_week=38.5,
        weekday_target_minutes=[495, 495, 495, 495, 330, 0, 0],
        employment_type="hourly",
        overtime_payout_mode="above_cap",
        overtime_cap_hours=20,
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    db.add(u)
    await db.flush()
    return u


def _utc(day: int, hhmm: str) -> datetime:
    """Lokale Berliner Zeit (Mai = CEST, UTC+2) -> UTC."""
    h, m = map(int, hhmm.split(":"))
    local_minutes = h * 60 + m - 120
    return datetime(2026, 5, day, local_minutes // 60, local_minutes % 60, tzinfo=UTC)


class TestMonthReconciliation:
    async def test_soll_kette_ledger_und_lohnarten(self, db, client, tenant, plan_employee) -> None:
        from zeiterfassung.services import clock_service

        now = datetime.now(UTC)
        # Katalog + Zuordnung wie im Export: 011 Zeitlohn, 129 Urlaub, 110 Feiertag.
        for nr, text in ((11, "Zeitlohn"), (129, "Urlaublohn"), (110, "Feiertagslohn")):
            db.add(
                WageTypeMapping(
                    tenant_id=tenant.id,
                    internal_code=f"LA_{nr:04d}",
                    datev_lohnart=nr,
                    description=text,
                    is_active=True,
                    unit="hours",
                    created_at=now,
                )
            )
        tenant.settings = {
            **(tenant.settings or {}),
            "zuschlaege": {"export_basis": "soll"},
            "lohnart_zuordnung": {
                "NORMALE_ARBEITSZEIT": "LA_0011",
                "URLAUB_STD": "LA_0129",
                "FEIERTAG_BEZAHLT": "LA_0110",
            },
        }
        # Mo 04.05. 06:00–14:30 ohne Pause -> ArbZG 30 min -> Ist 480.
        for etype, t in ((EventType.PUNCH_IN, "06:00"), (EventType.PUNCH_OUT, "14:30")):
            db.add(
                PunchEvent(
                    tenant_id=tenant.id,
                    employee_id=plan_employee.id,
                    event_type=etype,
                    event_time=_utc(4, t),
                    source=EventSource.NFC_TERMINAL,
                    created_at=now,
                )
            )
        # Di 05.05. Urlaub (ganztags).
        vac = AbsenceRequestCreate.model_construct(
            request_type=RequestType.VACATION,
            date_from=date(2026, 5, 5),
            date_to=date(2026, 5, 5),
            day_part=DayPart.FULL,
            hours=None,
            sick_status=None,
            payload=None,
            notes=None,
        )
        await create_request(
            db, tenant.id, plan_employee.id, vac, "HH", auto_approve=True, approver_id=uuid.uuid4()
        )
        await clock_service.recompute_daily_timesheet(
            db, tenant.id, plan_employee.id, date(2026, 5, 4)
        )
        # Ledger: April abgeschlossen (+5), Mai abgeschlossen (+10), Auszahlung 3, Korrektur +2.
        for ym, ed, typ, hrs in (
            (date(2026, 4, 1), date(2026, 4, 30), "accrual", "5"),
            (date(2026, 5, 1), date(2026, 5, 31), "accrual", "10"),
            (date(2026, 5, 1), date(2026, 5, 31), "payout", "-3"),
            (date(2026, 5, 1), date(2026, 5, 15), "adjustment", "2"),
        ):
            db.add(
                OvertimeEntry(
                    tenant_id=tenant.id,
                    employee_id=plan_employee.id,
                    year_month=ym,
                    entry_date=ed,
                    entry_type=typ,
                    hours=hrs,
                    reason="Test",
                    created_by=None,
                    created_at=now,
                )
            )
        await db.commit()

        r = await client.get(
            "/api/v1/admin/month-reconciliation",
            params={"year": 2026, "month": 5, "employee_id": str(plan_employee.id)},
            headers={"Authorization": f"Bearer {await _token(db, tenant.id, all_rights=True)}"},
        )
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["employee_number"] == "77"

        # Mai 2026 (HH): 16x Mo–Do (7920) + 5x Fr (1650) = Brutto 9570.
        # Feiertage auf Werktagen: Fr 01.05. (330), Do 14.05. (495), Mo 25.05. (495)
        # = 1320 min / 3 Tage.
        s = d["stunden"]
        assert s["brutto_soll_minutes"] == 9570
        assert s["feiertag_minutes"] == 1320
        assert s["feiertag_tage"] == 3.0
        assert s["urlaub_minutes"] == 495
        assert s["urlaub_tage"] == 1.0
        assert s["krank_minutes"] == 0
        assert s["sonstige_minutes"] == 0
        assert s["effektiv_soll_minutes"] == 9570 - 1320 - 495
        assert s["ist_minutes"] == 480
        assert s["saldo_minutes"] == 480 - (9570 - 1320 - 495)

        u = d["ueberstunden"]
        assert u["payout_mode"] == "above_cap"
        assert u["cap_hours"] == 20.0
        assert u["vortrag_hours"] == 5.0
        assert u["monat_hours"] == 10.0
        assert u["ausgezahlt_hours"] == 3.0
        assert u["korrektur_hours"] == 2.0
        assert u["verfall_hours"] == 0.0
        assert u["saldo_hours"] == 5 + 10 - 3 + 2
        assert u["abgeschlossen"] is True

        la = {row["lohnart"]: row for row in d["lohnarten"]}
        assert la[11]["wert"] == round((9570 - 1320 - 495) / 60, 2)
        assert la[11]["bezeichnung"] == "Zeitlohn"
        assert la[129]["wert"] == 8.25
        assert la[129]["tage"] == 1.0
        assert la[110]["wert"] == 22.0
        assert la[110]["tage"] == 3.0
        assert la[11]["tage"] is None
        assert d["urlaub_rest_tage"] is not None
        assert d["export_status"] is None

    async def test_offener_monat_ist_vorschau(self, db, client, tenant, plan_employee) -> None:
        await db.commit()
        r = await client.get(
            "/api/v1/admin/month-reconciliation",
            params={"year": 2026, "month": 5, "employee_id": str(plan_employee.id)},
            headers={"Authorization": f"Bearer {await _token(db, tenant.id, all_rights=True)}"},
        )
        assert r.status_code == 200, r.text
        u = r.json()["ueberstunden"]
        assert u["abgeschlossen"] is False
        # Ohne Stempel und ohne Ledger: Monatsvorschau = Ist − Soll = −Brutto (nach Feiertagen).
        assert u["monat_hours"] == round(-(9570 - 1320) / 60, 4)
        assert u["saldo_hours"] == u["monat_hours"]

    async def test_ohne_rechte_403(self, db, client, tenant, plan_employee) -> None:
        await db.commit()
        r = await client.get(
            "/api/v1/admin/month-reconciliation",
            params={"year": 2026, "month": 5, "employee_id": str(plan_employee.id)},
            headers={"Authorization": f"Bearer {await _token(db, tenant.id, all_rights=False)}"},
        )
        assert r.status_code == 403
