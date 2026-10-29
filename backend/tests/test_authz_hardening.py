"""Autorisierung härten: Rollen-/Tenant-Validierung + Department-Scope.

Deckt vier evidenzierte Autorisierungs-Luecken ab (Code-Review):

1. Konto-Rollenzuweisung: admin:accounts allein durfte role_id setzen
   (Privilegien-Eskalation), keine Selbst-Aenderung-Sperre, kein Tenant-Check.
2. UserRole.role_id war nicht Mandanten-geprueft.
3. department_id/company_id in create/update_user waren nicht Mandanten-geprueft
   (#100).
4. Absence revoke/sick-status ignorierten den Department-Scope von
   leave:approve_team (Teamleiter konnte fremde Bereiche bearbeiten).
5. /absences/feed.ics loeste den Tenant NICHT ueber den Token auf, sondern nahm
   immer den ersten Tenant der Tabelle.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from zeiterfassung.core.security import create_access_token, hash_password
from zeiterfassung.models.account import Account


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


async def _account(db, tenant, email, *, active=True, superadmin=False, employee_id=None):
    now = datetime.now(tz=UTC)
    acc = Account(
        tenant_id=tenant.id,
        email=email,
        hashed_password=hash_password("Passw0rd!"),
        is_active=active,
        is_superadmin=superadmin,
        employee_id=employee_id,
        created_at=now,
        updated_at=now,
    )
    db.add(acc)
    await db.commit()
    return acc


async def _role(db, tenant, name="Rolle", permissions=None):
    from zeiterfassung.models.user import Role

    now = datetime.now(tz=UTC)
    role = Role(
        tenant_id=tenant.id,
        name=name,
        permissions=permissions or [],
        data_scope="self",
        created_at=now,
        updated_at=now,
    )
    db.add(role)
    await db.commit()
    return role


async def _department(db, tenant, name="Lager", code="LAG"):
    from zeiterfassung.models.department import Department

    now = datetime.now(tz=UTC)
    dept = Department(
        tenant_id=tenant.id, name=name, code=code, is_active=True, created_at=now, updated_at=now
    )
    db.add(dept)
    await db.commit()
    return dept


async def _company(db, tenant, name="Firma"):
    from zeiterfassung.models.company import Company

    now = datetime.now(tz=UTC)
    co = Company(tenant_id=tenant.id, name=name, is_active=True, created_at=now, updated_at=now)
    db.add(co)
    await db.commit()
    return co


async def _tenant2(db):
    from zeiterfassung.models.tenant import Tenant

    now = datetime.now(tz=UTC)
    t = Tenant(
        name="Fremd-Mandant GmbH",
        timezone="Europe/Berlin",
        locale="de-DE",
        bundesland="HH",
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    db.add(t)
    await db.commit()
    return t


def _headers(
    tenant_id,
    account_id,
    *,
    superadmin=False,
    perms=(),
    department_id=None,
    department_ids=None,
    employee_id=None,
):
    tok = create_access_token(
        user_id=str(account_id),
        tenant_id=str(tenant_id),
        permissions=list(perms),
        department_id=str(department_id) if department_id else None,
        department_ids=[str(d) for d in department_ids] if department_ids else None,
        role_type="geschaeftsfuehrung",
        data_scope="global",
        is_superadmin=superadmin,
        employee_id=str(employee_id) if employee_id else None,
    )
    return {"Authorization": f"Bearer {tok}"}


# ---------------------------------------------------------------------------
# 1) Konto-Rollenzuweisung: admin:accounts allein reicht nicht mehr
# ---------------------------------------------------------------------------


class TestAccountRoleAssignmentAuthz:
    async def test_accounts_only_admin_cannot_set_role_on_create(self, db, client, tenant):
        me = await _account(db, tenant, "me@t.de")
        role = await _role(db, tenant)
        hdr = _headers(tenant.id, me.id, perms=("admin:accounts",))
        r = await client.post(
            "/api/v1/accounts",
            json={"email": "neu@t.de", "password": "Passw0rd!", "role_id": str(role.id)},
            headers=hdr,
        )
        assert r.status_code == 403, r.text

    async def test_accounts_only_admin_cannot_set_role_on_update(self, db, client, tenant):
        me = await _account(db, tenant, "me@t.de")
        ziel = await _account(db, tenant, "ziel@t.de")
        role = await _role(db, tenant)
        hdr = _headers(tenant.id, me.id, perms=("admin:accounts",))
        r = await client.patch(
            f"/api/v1/accounts/{ziel.id}", json={"role_id": str(role.id)}, headers=hdr
        )
        assert r.status_code == 403, r.text

    async def test_admin_roles_can_assign_role(self, db, client, tenant):
        me = await _account(db, tenant, "me@t.de")
        ziel = await _account(db, tenant, "ziel@t.de")
        role = await _role(db, tenant)
        hdr = _headers(tenant.id, me.id, perms=("admin:accounts", "admin:roles"))
        r = await client.patch(
            f"/api/v1/accounts/{ziel.id}", json={"role_id": str(role.id)}, headers=hdr
        )
        assert r.status_code == 200, r.text

    async def test_cannot_change_own_role(self, db, client, tenant):
        me = await _account(db, tenant, "me@t.de")
        role = await _role(db, tenant)
        hdr = _headers(tenant.id, me.id, perms=("admin:accounts", "admin:roles"))
        r = await client.patch(
            f"/api/v1/accounts/{me.id}", json={"role_id": str(role.id)}, headers=hdr
        )
        assert r.status_code == 409, r.text

    async def test_cross_tenant_role_id_rejected_on_create(self, db, client, tenant):
        other = await _tenant2(db)
        foreign_role = await _role(db, other, name="Fremd")
        me = await _account(db, tenant, "me@t.de")
        hdr = _headers(tenant.id, me.id, perms=("admin:accounts", "admin:roles"))
        r = await client.post(
            "/api/v1/accounts",
            json={
                "email": "neu@t.de",
                "password": "Passw0rd!",
                "role_id": str(foreign_role.id),
            },
            headers=hdr,
        )
        assert r.status_code == 404, r.text

    async def test_cross_tenant_role_id_rejected_on_update(self, db, client, tenant):
        other = await _tenant2(db)
        foreign_role = await _role(db, other, name="Fremd")
        me = await _account(db, tenant, "me@t.de")
        ziel = await _account(db, tenant, "ziel@t.de")
        hdr = _headers(tenant.id, me.id, perms=("admin:accounts", "admin:roles"))
        r = await client.patch(
            f"/api/v1/accounts/{ziel.id}", json={"role_id": str(foreign_role.id)}, headers=hdr
        )
        assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# 2) UserRole.role_id Mandanten-Check (POST/PATCH /users)
# ---------------------------------------------------------------------------


class TestUserRoleTenantCheck:
    async def test_cross_tenant_role_id_rejected_on_user_create(self, db, client, tenant):
        other = await _tenant2(db)
        foreign_role = await _role(db, other, name="Fremd")
        me = await _account(db, tenant, "me@t.de")
        hdr = _headers(tenant.id, me.id, perms=("users:write",))
        r = await client.post(
            "/api/v1/users",
            json={
                "email": "neu@t.de",
                "first_name": "A",
                "last_name": "B",
                "role_id": str(foreign_role.id),
            },
            headers=hdr,
        )
        assert r.status_code == 404, r.text

    async def test_cross_tenant_role_id_rejected_on_user_update(self, db, client, tenant, employee):
        other = await _tenant2(db)
        foreign_role = await _role(db, other, name="Fremd")
        me = await _account(db, tenant, "me@t.de")
        hdr = _headers(tenant.id, me.id, perms=("users:write",))
        r = await client.patch(
            f"/api/v1/users/{employee.id}",
            json={"role_id": str(foreign_role.id)},
            headers=hdr,
        )
        assert r.status_code == 404, r.text

    async def test_same_tenant_role_id_accepted(self, db, client, tenant, employee):
        role = await _role(db, tenant)
        me = await _account(db, tenant, "me@t.de")
        hdr = _headers(tenant.id, me.id, perms=("users:write",))
        r = await client.patch(
            f"/api/v1/users/{employee.id}",
            json={"role_id": str(role.id)},
            headers=hdr,
        )
        assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# 3) department_id/company_id Mandanten-Check (#100)
# ---------------------------------------------------------------------------


class TestUserDepartmentCompanyTenantCheck:
    async def test_cross_tenant_department_id_rejected(self, db, client, tenant, employee):
        other = await _tenant2(db)
        foreign_dept = await _department(db, other, name="Fremd-Bereich", code="FRD")
        me = await _account(db, tenant, "me@t.de")
        hdr = _headers(tenant.id, me.id, perms=("users:write",))
        r = await client.patch(
            f"/api/v1/users/{employee.id}",
            json={"department_id": str(foreign_dept.id)},
            headers=hdr,
        )
        assert r.status_code == 422, r.text

    async def test_cross_tenant_company_id_rejected(self, db, client, tenant, employee):
        other = await _tenant2(db)
        foreign_company = await _company(db, other, name="Fremd-Firma")
        me = await _account(db, tenant, "me@t.de")
        hdr = _headers(tenant.id, me.id, perms=("users:write",))
        r = await client.patch(
            f"/api/v1/users/{employee.id}",
            json={"company_id": str(foreign_company.id)},
            headers=hdr,
        )
        assert r.status_code == 422, r.text

    async def test_cross_tenant_department_id_rejected_on_create(self, db, client, tenant):
        other = await _tenant2(db)
        foreign_dept = await _department(db, other, name="Fremd-Bereich", code="FRD")
        me = await _account(db, tenant, "me@t.de")
        hdr = _headers(tenant.id, me.id, perms=("users:write",))
        r = await client.post(
            "/api/v1/users",
            json={
                "email": "neu@t.de",
                "first_name": "A",
                "last_name": "B",
                "department_id": str(foreign_dept.id),
            },
            headers=hdr,
        )
        assert r.status_code == 422, r.text

    async def test_same_tenant_department_id_accepted(self, db, client, tenant, employee):
        dept = await _department(db, tenant)
        me = await _account(db, tenant, "me@t.de")
        hdr = _headers(tenant.id, me.id, perms=("users:write",))
        r = await client.patch(
            f"/api/v1/users/{employee.id}",
            json={"department_id": str(dept.id)},
            headers=hdr,
        )
        assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# 4) Absence revoke/sick-status: Department-Scope fuer leave:approve_team
# ---------------------------------------------------------------------------


async def _create_absence_request(db, tenant, employee, *, approved=True):
    from zeiterfassung.models.absence import RequestType
    from zeiterfassung.policy.context import DayPart
    from zeiterfassung.schemas.absence import AbsenceRequestCreate
    from zeiterfassung.services.absence_service import create_request

    data = AbsenceRequestCreate.model_construct(
        request_type=RequestType.VACATION,
        date_from=date(2027, 8, 4),
        date_to=date(2027, 8, 6),
        day_part=DayPart.FULL,
        hours=None,
        payload=None,
        notes=None,
    )
    req = await create_request(
        db,
        tenant.id,
        employee.id,
        data,
        "HH",
        auto_approve=approved,
        approver_id=uuid.uuid4() if approved else None,
    )
    return req


class TestAbsenceDepartmentScope:
    async def test_teamleiter_cannot_revoke_outside_department(self, db, client, tenant):
        own_dept = await _department(db, tenant, name="Eigen", code="OWN")
        other_dept = await _department(db, tenant, name="Fremd", code="OTH")
        employee = await _make_employee(db, tenant, other_dept)
        req = await _create_absence_request(db, tenant, employee, approved=True)

        tl = await _account(db, tenant, "tl@t.de")
        hdr = _headers(
            tenant.id, tl.id, perms=("leave:approve_team",), department_ids=[own_dept.id]
        )
        r = await client.post(
            f"/api/v1/absences/requests/{req.id}/revoke", json={"reason": "x"}, headers=hdr
        )
        assert r.status_code == 403, r.text

    async def test_teamleiter_can_revoke_own_department(self, db, client, tenant):
        own_dept = await _department(db, tenant, name="Eigen2", code="OWN2")
        employee = await _make_employee(db, tenant, own_dept, email="ang2@t.de")
        req = await _create_absence_request(db, tenant, employee, approved=True)

        tl = await _account(db, tenant, "tl2@t.de")
        hdr = _headers(
            tenant.id, tl.id, perms=("leave:approve_team",), department_ids=[own_dept.id]
        )
        r = await client.post(
            f"/api/v1/absences/requests/{req.id}/revoke", json={"reason": "x"}, headers=hdr
        )
        assert r.status_code == 200, r.text

    async def test_teamleiter_cannot_set_sick_status_outside_department(self, db, client, tenant):
        from zeiterfassung.models.absence import RequestType
        from zeiterfassung.policy.context import DayPart
        from zeiterfassung.schemas.absence import AbsenceRequestCreate
        from zeiterfassung.services.absence_service import create_request

        own_dept = await _department(db, tenant, name="Eigen3", code="OWN3")
        other_dept = await _department(db, tenant, name="Fremd3", code="OTH3")
        employee = await _make_employee(db, tenant, other_dept, email="ang3@t.de")
        data = AbsenceRequestCreate.model_construct(
            request_type=RequestType.SICK_LEAVE,
            date_from=date(2027, 8, 9),
            date_to=date(2027, 8, 9),
            day_part=DayPart.FULL,
            hours=None,
            payload=None,
            notes=None,
        )
        req = await create_request(
            db, tenant.id, employee.id, data, "HH", auto_approve=True, approver_id=uuid.uuid4()
        )

        tl = await _account(db, tenant, "tl3@t.de")
        hdr = _headers(
            tenant.id, tl.id, perms=("leave:approve_team",), department_ids=[own_dept.id]
        )
        r = await client.patch(
            f"/api/v1/absences/requests/{req.id}/sick-status",
            json={"sick_status": "AU_MIT_LFZ"},
            headers=hdr,
        )
        assert r.status_code == 403, r.text

    async def test_gf_can_revoke_across_departments(self, db, client, tenant):
        other_dept = await _department(db, tenant, name="Fremd4", code="OTH4")
        employee = await _make_employee(db, tenant, other_dept, email="ang4@t.de")
        req = await _create_absence_request(db, tenant, employee, approved=True)

        gf = await _account(db, tenant, "gf@t.de")
        hdr = _headers(tenant.id, gf.id, perms=("leave:approve_all",))
        r = await client.post(
            f"/api/v1/absences/requests/{req.id}/revoke", json={"reason": "x"}, headers=hdr
        )
        assert r.status_code == 200, r.text


async def _make_employee(db, tenant, department, email="ang@t.de"):
    from zeiterfassung.models.user import User

    now = datetime.now(tz=UTC)
    u = User(
        tenant_id=tenant.id,
        email=email,
        hashed_password=hash_password("Passw0rd!"),
        first_name="Angestellt",
        last_name="Er",
        employee_number=email,
        contracted_hours_per_week=40.0,
        department_id=department.id,
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    db.add(u)
    await db.commit()
    return u


# ---------------------------------------------------------------------------
# 5) /absences/feed.ics loest den Mandanten ueber den Token auf
# ---------------------------------------------------------------------------


class TestIcsFeedTokenTenantResolution:
    async def test_feed_ics_token_of_tenant_b_does_not_serve_tenant_a(self, db, client, tenant):
        """Tenant A wird ZUERST angelegt (erste Zeile in `tenants`); Tenant B
        erhaelt einen Feed-Token. Vor dem Fix haette der Endpoint immer den
        ERSTEN Tenant (A) bedient, unabhaengig vom Token."""
        tenant_b = await _tenant2(db)
        tenant_b.settings = {"ics_feed_token": "geheim-b-token"}
        await db.commit()

        r = await client.get("/api/v1/absences/feed.ics", params={"token": "geheim-b-token"})
        assert r.status_code == 200, r.text
        # Der servierte Kalender muss zu Tenant B gehoeren, nicht Tenant A.
        # (Kein Employee bei B -> leerer, aber gueltiger ICS-Feed; Hauptsache
        # kein 403/falscher Mandant.)
        assert "BEGIN:VCALENDAR" in r.text

    async def test_feed_ics_unknown_token_403(self, db, client, tenant):
        tenant.settings = {"ics_feed_token": "richtiger-token"}
        await db.commit()
        r = await client.get("/api/v1/absences/feed.ics", params={"token": "falscher-token"})
        assert r.status_code == 403, r.text

    async def test_feed_ics_no_tenant_has_token_403(self, db, client, tenant):
        r = await client.get("/api/v1/absences/feed.ics", params={"token": "irgendwas"})
        assert r.status_code == 403, r.text
