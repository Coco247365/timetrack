"""Jahresauswertung pro Mitarbeiter (GET /admin/yearly-report, #29).

Prueft die Aggregation: 12 Monatszeilen aus Tagesboegen + Abwesenheiten,
Monats-Clipping ueber den Monatswechsel und die Abwesenheitsliste mit
AU-Status.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from zeiterfassung.core.security import create_access_token
from zeiterfassung.models.absence import RequestType, SickStatus
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


async def _gf_token(db: AsyncSession, tenant_id: uuid.UUID) -> str:
    from tests.conftest import ensure_login_account

    acc = await ensure_login_account(db, tenant_id)
    return create_access_token(
        user_id=str(acc.id),
        tenant_id=str(tenant_id),
        permissions=["timesheet:view_all", "admin:dashboard"],
        department_id=None,
        role_type="geschaeftsfuehrung",
        data_scope="global",
    )


async def _sick(db, tenant, employee, frm: date, to: date, sick_status=None):
    data = AbsenceRequestCreate.model_construct(
        request_type=RequestType.SICK_LEAVE,
        date_from=frm,
        date_to=to,
        day_part=DayPart.FULL,
        hours=None,
        sick_status=sick_status,
        payload=None,
        notes=None,
    )
    return await create_request(
        db, tenant.id, employee.id, data, "HH", auto_approve=True, approver_id=uuid.uuid4()
    )


class TestYearlyReport:
    async def test_monats_clipping_und_absence_liste(self, db, client, tenant, employee) -> None:
        # Krank über den Monatswechsel: Mo 29.06. – Do 02.07.2026
        # -> 2 Arbeitstage im Juni, 2 im Juli.
        await _sick(
            db, tenant, employee, date(2026, 6, 29), date(2026, 7, 2), SickStatus.AU_MIT_LFZ
        )
        # Urlaub im August: Mo 03.08. – Fr 07.08. -> 5 Tage.
        vac = AbsenceRequestCreate.model_construct(
            request_type=RequestType.VACATION,
            date_from=date(2026, 8, 3),
            date_to=date(2026, 8, 7),
            day_part=DayPart.FULL,
            hours=None,
            sick_status=None,
            payload=None,
            notes=None,
        )
        await create_request(
            db, tenant.id, employee.id, vac, "HH", auto_approve=True, approver_id=uuid.uuid4()
        )
        # Tagesbogen im Juni: 1 Arbeitstag mit 8h und 30 Min Überstunden.
        from zeiterfassung.models.clock import DailyTimesheet, TimesheetStatus

        db.add(
            DailyTimesheet(
                tenant_id=tenant.id,
                employee_id=employee.id,
                work_date=date(2026, 6, 15),
                gross_work_minutes=510,
                net_work_minutes=480,
                break_minutes=30,
                overtime_minutes=30,
                status=TimesheetStatus.COMPLETE,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        await db.commit()

        r = await client.get(
            "/api/v1/admin/yearly-report",
            params={"year": 2026, "employee_id": str(employee.id)},
            headers={"Authorization": f"Bearer {await _gf_token(db, tenant.id)}"},
        )
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["employee_name"] == "Max Mustermann"
        assert len(d["months"]) == 12

        juni = d["months"][5]
        juli = d["months"][6]
        aug = d["months"][7]
        assert juni["days_sick"] == 2.0  # 29.+30.06.
        assert juli["days_sick"] == 2.0  # 01.+02.07.
        assert aug["days_vacation"] == 5.0
        assert juni["days_worked"] == 1
        assert juni["work_minutes"] == 480
        # Saldo-Spalte = Monatssaldo der Konto-Rechnung (Ist − Soll, Minus-Tage
        # inklusive), nicht mehr die Summe der Plus-Tage aus dem Tagesbogen.
        from zeiterfassung.services import overtime_service

        erwartet = (await overtime_service.month_summary(db, tenant.id, employee, 2026, 6))[
            "saldo_minutes"
        ]
        assert juni["overtime_minutes"] == erwartet
        assert erwartet != 30

        # Abwesenheitsliste: 2 Einträge, Krank mit AU-Status.
        assert len(d["absences"]) == 2
        sick_entry = next(a for a in d["absences"] if a["request_type"] == "SICK_LEAVE")
        assert sick_entry["sick_status"] == "AU_MIT_LFZ"
        assert sick_entry["date_from"] == "2026-06-29"
        assert sick_entry["date_to"] == "2026-07-02"

    async def test_unbekannter_mitarbeiter_404(self, db, client, tenant, employee) -> None:
        r = await client.get(
            "/api/v1/admin/yearly-report",
            params={"year": 2026, "employee_id": str(uuid.uuid4())},
            headers={"Authorization": f"Bearer {await _gf_token(db, tenant.id)}"},
        )
        assert r.status_code == 404
