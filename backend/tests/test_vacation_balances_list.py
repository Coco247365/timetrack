"""Tests für GET /absences/balances — Resturlaub je Mitarbeiter (Mitarbeiterliste).

Prüft Rechte-Gate, Scope-Sichtbarkeit (GF = Mandant, Teamleiter = eigene
Abteilung) und dass der Wert dem Einzel-Saldo (`/absences/balance`) entspricht.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from zeiterfassung.core.security import create_access_token
from zeiterfassung.models.absence import RequestType
from zeiterfassung.models.department import Department
from zeiterfassung.models.tenant import Tenant
from zeiterfassung.models.user import User
from zeiterfassung.schemas.absence import AbsenceRequestCreate
from zeiterfassung.services.absence_service import (
    approve_request,
    create_request,
    get_vacation_balance,
)

pytestmark = pytest.mark.asyncio


async def _make_token(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    permissions: list[str],
    data_scope: str,
    role_type: str,
    department_id: uuid.UUID | None = None,
) -> str:
    from tests.conftest import ensure_login_account

    await ensure_login_account(db, tenant_id, account_id=user_id)
    return create_access_token(
        user_id=str(user_id),
        tenant_id=str(tenant_id),
        permissions=permissions,
        department_id=str(department_id) if department_id else None,
        role_type=role_type,
        data_scope=data_scope,
    )


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def app_with_db(engine):
    from sqlalchemy.ext.asyncio import AsyncSession as _AS
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from zeiterfassung.api import deps
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


@pytest_asyncio.fixture
async def tenant(db: AsyncSession) -> Tenant:
    now = datetime.now(tz=UTC)
    t = Tenant(
        name="Resturlaub Test GmbH",
        timezone="Europe/Berlin",
        locale="de-DE",
        bundesland="HH",
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    db.add(t)
    await db.flush()
    return t


@pytest_asyncio.fixture
async def departments(db: AsyncSession, tenant: Tenant) -> dict[str, Department]:
    now = datetime.now(tz=UTC)
    depts = {}
    for name, code in [("Lager", "LAG"), ("Verkauf", "VKF")]:
        d = Department(
            tenant_id=tenant.id,
            name=name,
            code=code,
            is_active=True,
            created_at=now,
            updated_at=now,
        )
        db.add(d)
        depts[name] = d
    await db.flush()
    return depts


@pytest_asyncio.fixture
async def users(
    db: AsyncSession, tenant: Tenant, departments: dict[str, Department]
) -> dict[str, User]:
    from zeiterfassung.core.security import hash_password

    now = datetime.now(tz=UTC)
    pw = hash_password("test123")
    specs = [
        ("Max", "Müller", "max@test.de", "Lager", True),
        ("Anna", "Schmidt", "anna@test.de", "Lager", True),
        ("Peter", "Becker", "peter@test.de", "Verkauf", True),
        ("Alt", "Ausgetreten", "alt@test.de", "Verkauf", False),
    ]
    result: dict[str, User] = {}
    for i, (first, last, email, dept, active) in enumerate(specs, start=1):
        u = User(
            tenant_id=tenant.id,
            email=email,
            hashed_password=pw,
            first_name=first,
            last_name=last,
            employee_number=f"EMP{i:03d}",
            contracted_hours_per_week=40.0,
            vacation_entitlement_days=30.0,
            is_active=active,
            department_id=departments[dept].id,
            created_at=now,
            updated_at=now,
        )
        db.add(u)
        result[email] = u
    await db.flush()
    return result


class TestBalancesList:
    async def test_gf_sees_all_active_employees(
        self, client: AsyncClient, db: AsyncSession, tenant: Tenant, users: dict
    ) -> None:
        token = await _make_token(
            db,
            tenant.id,
            users["max@test.de"].id,
            permissions=["timesheet:view_all"],
            data_scope="global",
            role_type="gf",
        )
        r = await client.get("/api/v1/absences/balances?year=2027", headers=_headers(token))
        assert r.status_code == 200, r.text
        data = r.json()
        # Inaktive/ausgetretene Mitarbeiter tauchen nicht auf.
        assert set(data) == {
            str(users["max@test.de"].id),
            str(users["anna@test.de"].id),
            str(users["peter@test.de"].id),
        }
        assert data[str(users["max@test.de"].id)] == 30.0

    async def test_approved_vacation_reduces_remaining(
        self, client: AsyncClient, db: AsyncSession, tenant: Tenant, users: dict
    ) -> None:
        # 2027-07-19 (Mo) bis 2027-07-23 (Fr) = 5 Arbeitstage
        req = await create_request(
            db,
            tenant.id,
            users["anna@test.de"].id,
            AbsenceRequestCreate(
                request_type=RequestType.VACATION,
                date_from=date(2027, 7, 19),
                date_to=date(2027, 7, 23),
            ),
        )
        await approve_request(db, tenant.id, req.id, uuid.uuid4())
        # Einzel-Saldo als Referenz — VOR dem HTTP-Aufruf: die Session der
        # App teilt sich mit dem Test die eine SQLite-Verbindung und rollt
        # beim Schließen die noch offene Test-Transaktion zurück.
        single = await get_vacation_balance(db, tenant.id, users["anna@test.de"].id, 2027)
        # Neue Definition (2026-08-18): Resturlaub = Anspruch − GENOMMENE Tage.
        # Der Urlaub liegt 2027 komplett in der Zukunft (geplant) und drueckt
        # den Rest deshalb nicht mehr — er ist "davon geplant".
        assert single.past_days == 0.0
        assert single.planned_days == 5.0
        assert single.remaining_days == 30.0

        token = await _make_token(
            db,
            tenant.id,
            users["max@test.de"].id,
            permissions=["timesheet:view_all"],
            data_scope="global",
            role_type="gf",
        )
        r = await client.get("/api/v1/absences/balances?year=2027", headers=_headers(token))
        assert r.status_code == 200, r.text
        # Deckungsgleich mit dem Einzel-Saldo im Mitarbeiterprofil.
        assert r.json()[str(users["anna@test.de"].id)] == single.remaining_days == 30.0

    async def test_teamleiter_sees_only_own_department(
        self, client: AsyncClient, db: AsyncSession, tenant: Tenant, users: dict, departments: dict
    ) -> None:
        token = await _make_token(
            db,
            tenant.id,
            users["max@test.de"].id,
            permissions=["timesheet:view_team"],
            data_scope="department",
            role_type="teamleiter",
            department_id=departments["Lager"].id,
        )
        r = await client.get("/api/v1/absences/balances?year=2027", headers=_headers(token))
        assert r.status_code == 200, r.text
        assert set(r.json()) == {
            str(users["max@test.de"].id),
            str(users["anna@test.de"].id),
        }

    async def test_without_permission_forbidden(
        self, client: AsyncClient, db: AsyncSession, tenant: Tenant, users: dict
    ) -> None:
        token = await _make_token(
            db,
            tenant.id,
            users["max@test.de"].id,
            permissions=["timesheet:view_own"],
            data_scope="self",
            role_type="mitarbeiter",
        )
        r = await client.get("/api/v1/absences/balances?year=2027", headers=_headers(token))
        assert r.status_code == 403

    async def test_no_token_returns_401(self, client: AsyncClient) -> None:
        r = await client.get("/api/v1/absences/balances?year=2027")
        assert r.status_code == 401
