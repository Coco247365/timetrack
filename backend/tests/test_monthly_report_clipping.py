"""Monatsbericht (#74): Halbtage zaehlen 0,5; Antraege werden auf den Monat geclippt."""

from __future__ import annotations

import uuid
from datetime import date

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from zeiterfassung.core.security import create_access_token
from zeiterfassung.models.absence import RequestType
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


async def _headers(db, tenant_id) -> dict[str, str]:
    from tests.conftest import ensure_login_account

    acc = await ensure_login_account(db, tenant_id)
    tok = create_access_token(
        user_id=str(acc.id),
        tenant_id=str(tenant_id),
        permissions=["admin:dashboard", "timesheet:view_all", "users:read_all"],
        department_id=None,
        role_type="geschaeftsfuehrung",
        data_scope="global",
    )
    return {"Authorization": f"Bearer {tok}"}


async def _absence(db, tenant, employee, frm, to, day_part=DayPart.FULL):
    data = AbsenceRequestCreate.model_construct(
        request_type=RequestType.VACATION,
        date_from=frm,
        date_to=to,
        day_part=day_part,
        hours=None,
        sick_status=None,
        payload=None,
        notes=None,
    )
    await create_request(
        db, tenant.id, employee.id, data, "HH", auto_approve=True, approver_id=uuid.uuid4()
    )


async def _report_row(client, db, tenant, employee, year, month):
    await db.commit()
    r = await client.get(
        f"/api/v1/admin/monthly-report?year={year}&month={month}",
        headers=await _headers(db, tenant.id),
    )
    assert r.status_code == 200, r.text
    rows = [x for x in r.json() if x["employee_id"] == str(employee.id)]
    assert rows, r.json()
    return rows[0]


class TestMonthlyReportClipping:
    async def test_halbtag_zaehlt_null_komma_fuenf(self, db, client, tenant, employee):
        await _absence(db, tenant, employee, date(2026, 6, 15), date(2026, 6, 15), DayPart.AM)
        row = await _report_row(client, db, tenant, employee, 2026, 6)
        assert row["days_vacation"] == 0.5

    async def test_antrag_ueber_monatsgrenze_wird_geclippt(self, db, client, tenant, employee):
        # 28.06.2026 (So) - 03.07.2026 (Fr): Juni-Anteil Mo 29.+Di 30. = 2,
        # Juli-Anteil Mi 01.-Fr 03. = 3.
        await _absence(db, tenant, employee, date(2026, 6, 28), date(2026, 7, 3))
        juni = await _report_row(client, db, tenant, employee, 2026, 6)
        juli = await _report_row(client, db, tenant, employee, 2026, 7)
        assert juni["days_vacation"] == 2.0
        assert juli["days_vacation"] == 3.0
