"""API-Tests fuer POST /admin/overtime/resettle (Monatsabschluss zuruecksetzen)."""

from __future__ import annotations

import uuid

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from zeiterfassung.core.security import create_access_token


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


async def _headers(
    db: AsyncSession, tenant_id: uuid.UUID, permissions: list[str]
) -> dict[str, str]:
    from tests.conftest import ensure_login_account

    acc = await ensure_login_account(db, tenant_id)
    tok = create_access_token(
        user_id=str(acc.id),
        tenant_id=str(tenant_id),
        permissions=permissions,
        department_id=None,
        role_type="geschaeftsfuehrung",
        data_scope="global",
    )
    return {"Authorization": f"Bearer {tok}"}


class TestResettleApi:
    async def test_zukunftsmonat_wird_abgelehnt(self, db, client, tenant, employee) -> None:
        r = await client.post(
            "/api/v1/admin/overtime/resettle",
            json={"year": 2099, "month": 1, "employee_id": str(employee.id)},
            headers=await _headers(db, tenant.id, ["admin:settings"]),
        )
        assert r.status_code == 422, r.text

    async def test_ohne_permission_wird_abgelehnt(self, db, client, tenant, employee) -> None:
        r = await client.post(
            "/api/v1/admin/overtime/resettle",
            json={"year": 2026, "month": 6, "employee_id": str(employee.id)},
            headers=await _headers(db, tenant.id, ["timesheet:read_own"]),
        )
        assert r.status_code == 403, r.text

    async def test_erfolgreicher_reset_einzelner_mitarbeiter(
        self, db, client, tenant, employee
    ) -> None:
        employee.overtime_payout_mode = "all"
        employee.counting_start_date = None
        await db.commit()

        r = await client.post(
            "/api/v1/admin/overtime/resettle",
            json={"year": 2026, "month": 6, "employee_id": str(employee.id)},
            headers=await _headers(db, tenant.id, ["overtime:manage"]),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert len(body) == 1
        assert body[0]["employee_id"] == str(employee.id)
        assert body[0]["employee_number"] == "EMP001"
