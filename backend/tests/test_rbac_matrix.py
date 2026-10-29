"""Comprehensive RBAC permission matrix tests.

Tests every endpoint x every role combination to catch permission leaks.

Role → permission mapping (from ADR-007):

| Permission              | GF | HR | Buchhaltung | Teamleiter | Mitarbeiter | BR |
|-------------------------|----|----|-------------|------------|-------------|-----|
| timesheet:view_all      | x  | x  |             |            |             |    |
| timesheet:view_team     |    |    |             | x          |             |    |
| timesheet:view_own      | x  | x  |             | x          | x           |    |
| timesheet:view_aggregates|   |    | x           |            |             | x  |
| leave:approve_all       | x  | x  |             |            |             |    |
| leave:approve_team      |    |    |             | x          |             |    |
| leave:request_own       | x  | x  |             | x          | x           |    |
| users:read_all          | x  | x  |             |            |             |    |
| users:read_team         |    |    |             | x          |             |    |
| users:read_own          | x  | x  | x           | x          | x           |    |
| users:write             | x  | x  |             |            |             |    |
| export:datev            | x  | x  | x           |            |             |    |
| export:own_data         | x  | x  | x           | x          | x           |    |
| admin:dashboard         | x  | x  | x           |            |             |    |
| admin:settings          | x  |    |             |            |             |    |
| audit:read              | x  | x  |             |            |             |    |
| audit:read_anonymized   |    |    |             |            |             | x  |
| medical:read            |    | x  |             |            |             |    |

Data scopes:
- GF (Geschäftsführung): global
- HR (Personalleitung): global
- Buchhaltung: global (but NO users:read_all/write, NO audit:read — only aggregates+export)
- Teamleiter: department
- Mitarbeiter: self
- Betriebsrat: anonymized_global
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from zeiterfassung.core.security import create_access_token
from zeiterfassung.models.tenant import Tenant
from zeiterfassung.models.user import User

# ---------------------------------------------------------------------------
# Role permission definitions (from ADR-007)
# ---------------------------------------------------------------------------

ROLE_PERMISSIONS: dict[str, dict[str, Any]] = {
    "geschaeftsfuehrung": {
        "permissions": [
            "timesheet:view_all",
            "timesheet:view_own",
            "leave:approve_all",
            "leave:request_own",
            "users:read_all",
            "users:write",
            "export:datev",
            "admin:dashboard",
            "admin:settings",
            "audit:read",
        ],
        "data_scope": "global",
        "role_type": "gf",
    },
    "hr": {
        "permissions": [
            "timesheet:view_all",
            "timesheet:view_own",
            "leave:approve_all",
            "leave:request_own",
            "users:read_all",
            "users:write",
            "export:datev",
            "admin:dashboard",
            "audit:read",
            "medical:read",
        ],
        "data_scope": "global",
        "role_type": "hr",
    },
    "buchhaltung": {
        "permissions": [
            "timesheet:view_aggregates",
            "export:datev",
            "admin:dashboard",
        ],
        "data_scope": "self",  # Buchhaltung sees only own personal data; aggregate/export access
        # is controlled by explicit permissions (export:datev, timesheet:view_aggregates),
        # not by data_scope. Setting to "global" would leak all scope-based filters (ADR-007).
        "role_type": "buchhaltung",
    },
    "teamleiter": {
        "permissions": [
            "timesheet:view_team",
            "timesheet:view_own",
            "leave:approve_team",
            "leave:request_own",
            "users:read_team",
        ],
        "data_scope": "department",
        "role_type": "teamleiter",
    },
    "mitarbeiter": {
        "permissions": [
            "timesheet:view_own",
            "leave:request_own",
        ],
        "data_scope": "self",
        "role_type": "mitarbeiter",
    },
    "betriebsrat": {
        "permissions": [
            "timesheet:view_aggregates",
            "audit:read_anonymized",
        ],
        "data_scope": "anonymized_global",
        "role_type": "betriebsrat",
    },
}


# ---------------------------------------------------------------------------
# Helpers to create test tokens
# ---------------------------------------------------------------------------


def _make_token(
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    role: str,
    department_id: uuid.UUID | None = None,
    employee_id: uuid.UUID | None = None,
) -> str:
    """Create a JWT for the given role using the real token factory.

    ``sub`` is the Account id; ``employee_id`` links to the person (User).
    """
    info = ROLE_PERMISSIONS[role]
    return create_access_token(
        user_id=str(account_id),
        tenant_id=str(tenant_id),
        permissions=info["permissions"],
        department_id=str(department_id) if department_id else None,
        role_type=info["role_type"],
        data_scope=info["data_scope"],
        employee_id=str(employee_id) if employee_id else None,
    )


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# App fixture with overridden DB
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def app_with_db(engine):
    """Return a FastAPI app instance with the test DB engine injected."""
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
    """Async HTTP test client."""
    async with AsyncClient(transport=ASGITransport(app=app_with_db), base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# Data fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def tenant(db: AsyncSession) -> Tenant:
    now = datetime.now(tz=UTC)
    t = Tenant(
        name="RBAC Test GmbH",
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
async def dept_id(db: AsyncSession, tenant: Tenant) -> uuid.UUID:
    """Create a department and return its ID."""
    from zeiterfassung.models.department import Department

    now = datetime.now(tz=UTC)
    dept = Department(
        tenant_id=tenant.id,
        name="Produktion",
        code="PROD",
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    db.add(dept)
    await db.flush()
    return dept.id


@pytest_asyncio.fixture
async def company_id(db: AsyncSession, tenant: Tenant) -> uuid.UUID:
    """A company with DATEV advisor/client numbers (required for exports)."""
    from zeiterfassung.models.company import Company

    now = datetime.now(tz=UTC)
    co = Company(
        tenant_id=tenant.id,
        name="Demo GmbH",
        is_active=True,
        advisor_number="12345",
        client_number="54321",
        created_at=now,
        updated_at=now,
    )
    db.add(co)
    await db.flush()
    return co.id


@pytest_asyncio.fixture
async def employee_user(
    db: AsyncSession, tenant: Tenant, dept_id: uuid.UUID, company_id: uuid.UUID
) -> User:
    """A regular mitarbeiter user (assigned to the DATEV company)."""
    from zeiterfassung.core.security import hash_password

    now = datetime.now(tz=UTC)
    u = User(
        tenant_id=tenant.id,
        email="mitarbeiter@test.de",
        hashed_password=hash_password("Passw0rd!"),
        first_name="Klaus",
        last_name="Müller",
        employee_number="EMP001",
        contracted_hours_per_week=40.0,
        is_active=True,
        department_id=dept_id,
        company_id=company_id,
        created_at=now,
        updated_at=now,
    )
    db.add(u)
    await db.flush()
    return u


@pytest_asyncio.fixture
async def account(db: AsyncSession, tenant: Tenant, employee_user: User):
    """A login Account linked to the employee (JWT sub = account id)."""
    from zeiterfassung.core.security import hash_password
    from zeiterfassung.models.account import Account

    now = datetime.now(tz=UTC)
    acc = Account(
        tenant_id=tenant.id,
        email=employee_user.email,
        hashed_password=hash_password("Passw0rd!"),
        is_superadmin=False,
        employee_id=employee_user.id,
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    db.add(acc)
    await db.flush()
    return acc


@pytest_asyncio.fixture
async def tokens(
    tenant: Tenant, employee_user: User, dept_id: uuid.UUID, account
) -> dict[str, str]:
    """Return a dict of role -> Bearer token for the employee's account."""
    return {
        role: _make_token(tenant.id, account.id, role, dept_id, employee_id=employee_user.id)
        for role in ROLE_PERMISSIONS
    }


# ---------------------------------------------------------------------------
# Absence request fixture (needed for approve/reject/cancel endpoints)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def absence_request_id(db: AsyncSession, tenant: Tenant, employee_user: User) -> uuid.UUID:
    """Create a submitted absence request, return its ID."""
    from datetime import date

    from zeiterfassung.models.absence import RequestType
    from zeiterfassung.schemas.absence import AbsenceRequestCreate
    from zeiterfassung.services.absence_service import create_request

    data = AbsenceRequestCreate(
        request_type=RequestType.VACATION,
        date_from=date(2027, 8, 4),
        date_to=date(2027, 8, 8),
    )
    req = await create_request(db, tenant.id, employee_user.id, data)
    await db.flush()
    return req.id


# ---------------------------------------------------------------------------
# Export record fixture
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def export_id(db: AsyncSession, tenant: Tenant, employee_user: User) -> uuid.UUID:
    """Create a payroll export record."""
    from datetime import date

    from zeiterfassung.models.export import ExportStatus, PayrollExport

    now = datetime.now(tz=UTC)
    exp = PayrollExport(
        tenant_id=tenant.id,
        year_month=date(2026, 3, 1),
        generated_by=employee_user.id,
        status=ExportStatus.GENERATED,
        row_count=1,
        created_at=now,
        updated_at=now,
    )
    db.add(exp)
    await db.flush()
    return exp.id


# ===========================================================================
# RBAC Matrix Tests
# ===========================================================================


class TestAuthEndpoints:
    """Auth endpoints — no role required (public or any-authenticated)."""

    async def test_login_no_auth_required(self, client: AsyncClient) -> None:
        """POST /auth/login is public (no token needed)."""
        r = await client.post(
            "/api/v1/auth/login",
            json={"email": "x@y.de", "password": "wrong"},
        )
        # 401 (bad creds) or 400 (multi-tenant), not 403
        assert r.status_code in (400, 401)

    async def test_logout_without_credentials_is_still_204(self, client: AsyncClient) -> None:
        """POST /auth/logout never requires an access token (fkw.4).

        No cookie, no body (the body-fallback is removed, fkw.4/Phase 6) —
        best-effort semantics mean this is still 204, not 401.
        """
        r = await client.post("/api/v1/auth/logout")
        assert r.status_code == 204

    @pytest.mark.parametrize("role", list(ROLE_PERMISSIONS.keys()))
    async def test_auth_me_allowed_for_all_roles(
        self,
        client: AsyncClient,
        tokens: dict,
        db: AsyncSession,
        tenant: Tenant,
        employee_user: User,
        role: str,
    ) -> None:
        """GET /auth/me is allowed for every authenticated role."""
        r = await client.get("/api/v1/auth/me", headers=_headers(tokens[role]))
        assert r.status_code == 200, (
            f"Role {role} should access /auth/me, got {r.status_code}: {r.text}"
        )


class TestUsersEndpoints:
    """User management endpoints — strict RBAC."""

    # GET /users — list users
    @pytest.mark.parametrize(
        "role,expected",
        [
            ("geschaeftsfuehrung", 200),
            ("hr", 200),
            ("buchhaltung", 200),  # sees only own (via scope), not forbidden
            ("teamleiter", 200),  # sees team (via scope), not forbidden
            ("mitarbeiter", 200),  # sees only self (via scope), not forbidden
            ("betriebsrat", 200),  # gets empty list (anonymized scope), not forbidden
        ],
    )
    async def test_list_users(
        self, client: AsyncClient, tokens: dict, role: str, expected: int
    ) -> None:
        """All roles can call GET /users — result is scoped, not forbidden."""
        r = await client.get("/api/v1/users", headers=_headers(tokens[role]))
        assert r.status_code == expected, f"Role {role} GET /users: {r.status_code} {r.text}"

    # POST /users — create user
    @pytest.mark.parametrize(
        "role,expected",
        [
            ("geschaeftsfuehrung", 409),  # auth OK, duplicate email → 409
            ("hr", 409),
            ("buchhaltung", 403),  # must NOT create users
            ("teamleiter", 403),
            ("mitarbeiter", 403),
            ("betriebsrat", 403),
        ],
    )
    async def test_create_user_permissions(
        self, client: AsyncClient, tokens: dict, employee_user: User, role: str, expected: int
    ) -> None:
        """Only GF and HR can create users.

        We send a valid body (intentional duplicate email) so Pydantic validation
        passes for all roles. Roles without permission get 403. Roles with permission
        get 409 (duplicate email).
        """
        body = {
            "email": employee_user.email,  # deliberate duplicate → 409 for authorized roles
            "password": "Passw0rd!",
            "first_name": "Test",
            "last_name": "User",
            "contracted_hours_per_week": 40.0,
        }
        r = await client.post("/api/v1/users", json=body, headers=_headers(tokens[role]))
        # 409 = auth passed, body valid, but email already exists (correct role)
        # 403 = access denied (wrong role)
        assert r.status_code == expected, f"Role {role} POST /users: {r.status_code} {r.text}"

    # GET /users/me — own profile
    @pytest.mark.parametrize("role", list(ROLE_PERMISSIONS.keys()))
    async def test_get_own_profile_allowed_for_all(
        self, client: AsyncClient, tokens: dict, role: str
    ) -> None:
        """GET /users/me is allowed for all authenticated roles."""
        r = await client.get("/api/v1/users/me", headers=_headers(tokens[role]))
        assert r.status_code == 200, f"Role {role} GET /users/me: {r.status_code} {r.text}"

    # GET /users/me/export — DSGVO self-export
    @pytest.mark.parametrize("role", list(ROLE_PERMISSIONS.keys()))
    async def test_own_data_export_allowed_for_all(
        self, client: AsyncClient, tokens: dict, role: str
    ) -> None:
        """DSGVO Art. 15 self-export is allowed for all authenticated roles."""
        r = await client.get("/api/v1/users/me/export", headers=_headers(tokens[role]))
        assert r.status_code == 200, f"Role {role} GET /users/me/export: {r.status_code} {r.text}"

    # GET /users/{user_id} — get specific user
    @pytest.mark.parametrize(
        "role,expected",
        [
            ("geschaeftsfuehrung", 200),  # users:read_all
            ("hr", 200),  # users:read_all
            ("buchhaltung", 200),  # own ID — always allowed
            ("teamleiter", 200),  # own ID — always allowed
            ("mitarbeiter", 200),  # own ID — always allowed
            ("betriebsrat", 200),  # own ID — always allowed
        ],
    )
    async def test_get_own_user_by_id(
        self, client: AsyncClient, tokens: dict, employee_user: User, role: str, expected: int
    ) -> None:
        """GET /users/{own_id} is always allowed."""
        r = await client.get(f"/api/v1/users/{employee_user.id}", headers=_headers(tokens[role]))
        assert r.status_code == expected, (
            f"Role {role} GET /users/{{own_id}}: {r.status_code} {r.text}"
        )

    # PATCH /users/{user_id} — update user
    @pytest.mark.parametrize(
        "role,expected",
        [
            ("geschaeftsfuehrung", 200),  # users:write
            ("hr", 200),  # users:write
            ("buchhaltung", 403),  # LEAK: must NOT update users
            ("teamleiter", 403),
            ("mitarbeiter", 403),
            ("betriebsrat", 403),
        ],
    )
    async def test_update_user_permissions(
        self, client: AsyncClient, tokens: dict, employee_user: User, role: str, expected: int
    ) -> None:
        """Only GF and HR can update users."""
        r = await client.patch(
            f"/api/v1/users/{employee_user.id}",
            json={"first_name": "Updated"},
            headers=_headers(tokens[role]),
        )
        assert r.status_code == expected, (
            f"Role {role} PATCH /users/{{id}}: {r.status_code} {r.text}"
        )


class TestAdminEndpoints:
    """Admin dashboard endpoints."""

    # GET /admin/dashboard
    @pytest.mark.parametrize(
        "role,expected",
        [
            ("geschaeftsfuehrung", 200),
            ("hr", 200),
            ("buchhaltung", 200),  # admin:dashboard allowed per ADR-007
            ("teamleiter", 403),  # LEAK: teamleiter must NOT see dashboard
            ("mitarbeiter", 403),
            ("betriebsrat", 403),  # LEAK: BR must NOT see dashboard stats
        ],
    )
    async def test_dashboard_permissions(
        self, client: AsyncClient, tokens: dict, role: str, expected: int
    ) -> None:
        r = await client.get("/api/v1/admin/dashboard", headers=_headers(tokens[role]))
        assert r.status_code == expected, (
            f"Role {role} GET /admin/dashboard: {r.status_code} {r.text}"
        )

    # GET /admin/flagged — flagged timesheets
    @pytest.mark.parametrize(
        "role,expected",
        [
            ("geschaeftsfuehrung", 200),
            ("hr", 200),
            # Buchhaltung must NOT see flagged timesheets — these contain identifiable
            # per-employee data. Buchhaltung has admin:dashboard but only for the
            # summary stats endpoint. Flagged timesheets require timesheet:view_all (ADR-007).
            ("buchhaltung", 403),
            ("teamleiter", 403),  # teamleiter does NOT have timesheet:view_all
            ("mitarbeiter", 403),
            ("betriebsrat", 403),  # BR must NOT see flagged timesheets (identifiable data)
        ],
    )
    async def test_flagged_timesheets_permissions(
        self, client: AsyncClient, tokens: dict, role: str, expected: int
    ) -> None:
        r = await client.get("/api/v1/admin/flagged", headers=_headers(tokens[role]))
        assert r.status_code == expected, (
            f"Role {role} GET /admin/flagged: {r.status_code} {r.text}"
        )

    # GET /admin/pending-approvals
    @pytest.mark.parametrize(
        "role,expected",
        [
            ("geschaeftsfuehrung", 200),
            ("hr", 200),
            ("buchhaltung", 403),  # LEAK: buchhaltung has NO approval permissions
            ("teamleiter", 200),  # leave:approve_team
            ("mitarbeiter", 403),
            ("betriebsrat", 403),
        ],
    )
    async def test_pending_approvals_permissions(
        self, client: AsyncClient, tokens: dict, role: str, expected: int
    ) -> None:
        r = await client.get("/api/v1/admin/pending-approvals", headers=_headers(tokens[role]))
        assert r.status_code == expected, (
            f"Role {role} GET /admin/pending-approvals: {r.status_code} {r.text}"
        )

    # POST /admin/bulk-approve
    @pytest.mark.parametrize(
        "role,expected",
        [
            ("geschaeftsfuehrung", 200),  # auth OK, empty list → 200 with [] response
            ("hr", 200),
            ("buchhaltung", 403),  # buchhaltung has NO leave:approve_all
            ("teamleiter", 403),  # only leave:approve_team, not approve_all
            ("mitarbeiter", 403),
            ("betriebsrat", 403),
        ],
    )
    async def test_bulk_approve_permissions(
        self, client: AsyncClient, tokens: dict, role: str, expected: int
    ) -> None:
        r = await client.post(
            "/api/v1/admin/bulk-approve",
            json={"request_ids": []},
            headers=_headers(tokens[role]),
        )
        assert r.status_code == expected, (
            f"Role {role} POST /admin/bulk-approve: {r.status_code} {r.text}"
        )

    # GET /admin/monthly-report
    @pytest.mark.parametrize(
        "role,expected",
        [
            ("geschaeftsfuehrung", 200),
            ("hr", 200),
            ("buchhaltung", 200),  # admin:dashboard allows monthly-report
            ("teamleiter", 200),  # timesheet:view_team
            ("mitarbeiter", 403),
            ("betriebsrat", 403),  # LEAK: BR must NOT see per-employee monthly report
        ],
    )
    async def test_monthly_report_permissions(
        self, client: AsyncClient, tokens: dict, role: str, expected: int
    ) -> None:
        r = await client.get(
            "/api/v1/admin/monthly-report",
            params={"year": 2026, "month": 3},
            headers=_headers(tokens[role]),
        )
        assert r.status_code == expected, (
            f"Role {role} GET /admin/monthly-report: {r.status_code} {r.text}"
        )

    # GET /admin/yearly-report (Jahresauswertung pro Mitarbeiter, #29)
    @pytest.mark.parametrize(
        "role,expected",
        [
            ("geschaeftsfuehrung", 200),
            ("hr", 200),
            ("buchhaltung", 200),  # admin:dashboard — wie monthly-report
            ("teamleiter", 200),  # timesheet:view_team, MA im eigenen Bereich
            ("mitarbeiter", 403),
            ("betriebsrat", 403),  # LEAK: BR darf keine Personen-Auswertung sehen
        ],
    )
    async def test_yearly_report_permissions(
        self, client: AsyncClient, tokens: dict, role: str, expected: int, employee_user
    ) -> None:
        r = await client.get(
            "/api/v1/admin/yearly-report",
            params={"year": 2026, "employee_id": str(employee_user.id)},
            headers=_headers(tokens[role]),
        )
        assert r.status_code == expected, (
            f"Role {role} GET /admin/yearly-report: {r.status_code} {r.text}"
        )

    # GET /admin/audit-log
    @pytest.mark.parametrize(
        "role,expected",
        [
            ("geschaeftsfuehrung", 200),
            ("hr", 200),
            ("buchhaltung", 403),  # LEAK: buchhaltung has NO audit:read (only export:datev)
            ("teamleiter", 403),
            ("mitarbeiter", 403),
            ("betriebsrat", 403),  # BR uses /br/audit-trail, NOT the full audit log
        ],
    )
    async def test_audit_log_permissions(
        self, client: AsyncClient, tokens: dict, role: str, expected: int
    ) -> None:
        r = await client.get("/api/v1/admin/audit-log", headers=_headers(tokens[role]))
        assert r.status_code == expected, (
            f"Role {role} GET /admin/audit-log: {r.status_code} {r.text}"
        )

    # GET /admin/approval-context/{request_id}
    @pytest.mark.parametrize(
        "role,expected",
        [
            ("geschaeftsfuehrung", 200),
            ("hr", 200),
            ("buchhaltung", 403),  # LEAK: buchhaltung has NO approval permissions
            ("teamleiter", 200),  # leave:approve_team
            ("mitarbeiter", 403),
            ("betriebsrat", 403),
        ],
    )
    async def test_approval_context_permissions(
        self,
        client: AsyncClient,
        tokens: dict,
        absence_request_id: uuid.UUID,
        role: str,
        expected: int,
    ) -> None:
        r = await client.get(
            f"/api/v1/admin/approval-context/{absence_request_id}",
            headers=_headers(tokens[role]),
        )
        assert r.status_code == expected, (
            f"Role {role} GET /admin/approval-context/{{id}}: {r.status_code} {r.text}"
        )


class TestAbsenceEndpoints:
    """Absence / leave request endpoints."""

    # POST /absences/requests
    @pytest.mark.parametrize(
        "role,expected",
        [
            ("geschaeftsfuehrung", 201),
            ("hr", 201),
            ("buchhaltung", 403),  # LEAK: buchhaltung has NO leave:request_own
            ("teamleiter", 201),
            ("mitarbeiter", 201),
            ("betriebsrat", 403),  # BR cannot submit leave
        ],
    )
    async def test_create_absence_request_permissions(
        self, client: AsyncClient, tokens: dict, role: str, expected: int
    ) -> None:

        body = {
            "request_type": "VACATION",
            "date_from": "2027-09-01",
            "date_to": "2027-09-05",
        }
        r = await client.post(
            "/api/v1/absences/requests", json=body, headers=_headers(tokens[role])
        )
        assert r.status_code == expected, (
            f"Role {role} POST /absences/requests: {r.status_code} {r.text}"
        )

    # GET /absences/requests
    @pytest.mark.parametrize("role", list(ROLE_PERMISSIONS.keys()))
    async def test_list_absence_requests_no_forbidden(
        self, client: AsyncClient, tokens: dict, role: str
    ) -> None:
        """All roles can call GET /absences/requests — result is scope-filtered."""
        r = await client.get("/api/v1/absences/requests", headers=_headers(tokens[role]))
        assert r.status_code == 200, f"Role {role} GET /absences/requests: {r.status_code} {r.text}"

    # GET /absences/requests/{id}
    @pytest.mark.parametrize(
        "role,expected",
        [
            ("geschaeftsfuehrung", 200),  # global scope, has leave:approve_all
            ("hr", 200),  # global scope, has leave:approve_all
            # buchhaltung has no leave permission → 404 (existence not revealed per ADR-007)
            ("buchhaltung", 404),
            ("teamleiter", 200),  # dept scope, has leave:approve_team
            ("mitarbeiter", 200),  # has leave:request_own
            # betriebsrat has no leave permissions → 404
            ("betriebsrat", 404),
        ],
    )
    async def test_get_absence_request_by_id(
        self,
        client: AsyncClient,
        tokens: dict,
        absence_request_id: uuid.UUID,
        employee_user: User,
        role: str,
        expected: int,
    ) -> None:
        """Access to a specific absence request is permission-gated.

        Roles without any leave permission (buchhaltung, betriebsrat) get 404 — the
        existence of the request is not revealed to unauthorized roles.
        """
        r = await client.get(
            f"/api/v1/absences/requests/{absence_request_id}",
            headers=_headers(tokens[role]),
        )
        assert r.status_code == expected, (
            f"Role {role} GET /absences/requests/{{id}}: {r.status_code} {r.text}"
        )

    # PATCH /absences/requests/{id}/approve
    @pytest.mark.parametrize(
        "role,expected",
        [
            ("geschaeftsfuehrung", 403),  # 403 because self-approval is blocked (same user)
            ("hr", 403),  # self-approval blocked
            ("buchhaltung", 403),  # NO approval permission
            ("teamleiter", 403),  # leave:approve_team but self-approval blocked
            ("mitarbeiter", 403),  # NO approval permission
            ("betriebsrat", 403),  # NO approval permission
        ],
    )
    async def test_approve_request_permissions(
        self,
        client: AsyncClient,
        tokens: dict,
        absence_request_id: uuid.UUID,
        role: str,
        expected: int,
    ) -> None:
        """Approve requires leave:approve_* and blocks self-approval."""
        r = await client.patch(
            f"/api/v1/absences/requests/{absence_request_id}/approve",
            json={"reason": "approved"},
            headers=_headers(tokens[role]),
        )
        assert r.status_code == expected, (
            f"Role {role} PATCH /absences/requests/{{id}}/approve: {r.status_code} {r.text}"
        )

    # PATCH /absences/requests/{id}/reject
    @pytest.mark.parametrize(
        "role,expected",
        [
            ("geschaeftsfuehrung", "not_403"),  # has approve_all — can reject (gets 404 or 200)
            ("hr", "not_403"),
            ("buchhaltung", "403_or_404"),  # no leave permission → 403
            ("teamleiter", "not_403"),  # has approve_team — can reject (no self-reject check)
            ("mitarbeiter", "403_or_404"),  # no approval permission → 403
            ("betriebsrat", "403_or_404"),  # no leave permission → 404
        ],
    )
    async def test_reject_request_permissions(
        self,
        client: AsyncClient,
        tokens: dict,
        absence_request_id: uuid.UUID,
        role: str,
        expected: str,
    ) -> None:
        """Reject requires leave:approve_* permission; buchhaltung/BR get 403 or 404."""
        r = await client.patch(
            f"/api/v1/absences/requests/{absence_request_id}/reject",
            json={"reason": "rejected"},
            headers=_headers(tokens[role]),
        )
        if expected == "not_403":
            # Role has approval permission — should NOT get 403 (may get 200, 404, 409)
            assert r.status_code != 403, (
                f"Role {role} should NOT get 403, got {r.status_code} {r.text}"
            )
        else:
            # Role has no approval permission — should get 403 or 404
            assert r.status_code in (403, 404), (
                f"Role {role} should get 403 or 404, got {r.status_code} {r.text}"
            )

    # PATCH /absences/requests/{id}/cancel
    @pytest.mark.parametrize(
        "role,expected",
        [
            ("geschaeftsfuehrung", "allow"),  # leave:request_own
            ("hr", "allow"),
            ("buchhaltung", "deny"),  # no leave:request_own → 403
            ("teamleiter", "allow"),
            ("mitarbeiter", "allow"),
            ("betriebsrat", "deny"),  # no leave permission → 403
        ],
    )
    async def test_cancel_own_request_permissions(
        self,
        client: AsyncClient,
        tokens: dict,
        absence_request_id: uuid.UUID,
        role: str,
        expected: str,
    ) -> None:
        """Cancel requires leave:request_own. Buchhaltung/betriebsrat get 403."""
        r = await client.patch(
            f"/api/v1/absences/requests/{absence_request_id}/cancel",
            headers=_headers(tokens[role]),
        )
        if expected == "deny":
            assert r.status_code == 403, (
                f"Role {role} should get 403 on cancel, got {r.status_code} {r.text}"
            )
        else:
            assert r.status_code in (200, 404, 409), (
                f"Role {role} cancel: unexpected {r.status_code} {r.text}"
            )

    # GET /absences/balance
    @pytest.mark.parametrize("role", list(ROLE_PERMISSIONS.keys()))
    async def test_own_balance_allowed_for_all(
        self, client: AsyncClient, tokens: dict, role: str
    ) -> None:
        """All roles can check their own vacation balance."""
        r = await client.get(
            "/api/v1/absences/balance",
            params={"year": 2026},
            headers=_headers(tokens[role]),
        )
        assert r.status_code == 200, f"Role {role} GET /absences/balance: {r.status_code} {r.text}"

    # GET /absences/calendar
    @pytest.mark.parametrize("role", list(ROLE_PERMISSIONS.keys()))
    async def test_calendar_all_roles(self, client: AsyncClient, tokens: dict, role: str) -> None:
        """All roles can call the calendar — result is scope-filtered."""
        r = await client.get(
            "/api/v1/absences/calendar",
            params={"date_from": "2026-01-01", "date_to": "2026-12-31"},
            headers=_headers(tokens[role]),
        )
        assert r.status_code == 200, f"Role {role} GET /absences/calendar: {r.status_code} {r.text}"


class TestClockEndpoints:
    """Clock in/out endpoints."""

    # POST /clock/punch
    @pytest.mark.parametrize(
        "role,expected",
        [
            ("geschaeftsfuehrung", 201),
            ("hr", 201),
            ("buchhaltung", 403),  # LEAK: buchhaltung has no clock permission
            ("teamleiter", 201),
            ("mitarbeiter", 201),
            ("betriebsrat", 403),  # LEAK: BR cannot clock in/out
        ],
    )
    async def test_punch_permissions(
        self, client: AsyncClient, tokens: dict, role: str, expected: int
    ) -> None:
        body = {"event_type": "PUNCH_IN", "source": "WEB_PORTAL"}
        r = await client.post("/api/v1/clock/punch", json=body, headers=_headers(tokens[role]))
        assert r.status_code == expected, f"Role {role} POST /clock/punch: {r.status_code} {r.text}"

    # GET /clock/status
    @pytest.mark.parametrize(
        "role,expected",
        [
            ("geschaeftsfuehrung", 200),
            ("hr", 200),
            ("buchhaltung", 403),  # LEAK: buchhaltung has no clock permission
            ("teamleiter", 200),
            ("mitarbeiter", 200),
            ("betriebsrat", 403),  # LEAK: BR should not see clock status
        ],
    )
    async def test_clock_status_permissions(
        self, client: AsyncClient, tokens: dict, role: str, expected: int
    ) -> None:
        r = await client.get("/api/v1/clock/status", headers=_headers(tokens[role]))
        assert r.status_code == expected, f"Role {role} GET /clock/status: {r.status_code} {r.text}"

    # GET /clock/presence
    @pytest.mark.parametrize(
        "role,expected",
        [
            ("geschaeftsfuehrung", 200),  # timesheet:view_all
            ("hr", 200),  # timesheet:view_all
            ("buchhaltung", 403),  # LEAK: no view_all/view_team
            ("teamleiter", 200),  # timesheet:view_team
            ("mitarbeiter", 403),  # LEAK: no team visibility
            ("betriebsrat", 403),  # LEAK: no individual presence data
        ],
    )
    async def test_presence_permissions(
        self, client: AsyncClient, tokens: dict, role: str, expected: int
    ) -> None:
        r = await client.get("/api/v1/clock/presence", headers=_headers(tokens[role]))
        assert r.status_code == expected, (
            f"Role {role} GET /clock/presence: {r.status_code} {r.text}"
        )

    # GET /clock/timesheet
    @pytest.mark.parametrize("role", list(ROLE_PERMISSIONS.keys()))
    async def test_get_own_timesheet_all_roles(
        self, client: AsyncClient, tokens: dict, role: str
    ) -> None:
        """GET /clock/timesheet is scoped — all roles get their visible timesheets."""
        r = await client.get(
            "/api/v1/clock/timesheet",
            params={"date_from": "2026-01-01", "date_to": "2026-03-31"},
            headers=_headers(tokens[role]),
        )
        assert r.status_code == 200, f"Role {role} GET /clock/timesheet: {r.status_code} {r.text}"

    # GET /clock/timesheet/{employee_id}
    @pytest.mark.parametrize(
        "role,expected",
        [
            ("geschaeftsfuehrung", 200),  # timesheet:view_all
            ("hr", 200),
            ("buchhaltung", 403),  # LEAK: has only timesheet:view_aggregates, not view_all
            ("teamleiter", 200),  # timesheet:view_team (same dept)
            ("mitarbeiter", 200),  # own ID shortcircuit
            ("betriebsrat", 403),  # no timesheet:view_all or view_team
        ],
    )
    async def test_get_employee_timesheet_permissions(
        self, client: AsyncClient, tokens: dict, employee_user: User, role: str, expected: int
    ) -> None:
        r = await client.get(
            f"/api/v1/clock/timesheet/{employee_user.id}",
            params={"date_from": "2026-01-01", "date_to": "2026-03-31"},
            headers=_headers(tokens[role]),
        )
        assert r.status_code == expected, (
            f"Role {role} GET /clock/timesheet/{{emp_id}}: {r.status_code} {r.text}"
        )

    # POST /clock/recompute/{work_date} — admin only
    @pytest.mark.parametrize(
        "role,expected",
        [
            ("geschaeftsfuehrung", 200),  # timesheet:view_all allows recompute
            ("hr", 200),
            ("buchhaltung", 403),  # admin:dashboard does NOT grant recompute
            ("teamleiter", 403),
            ("mitarbeiter", 403),
            ("betriebsrat", 403),
        ],
    )
    async def test_recompute_timesheet_permissions(
        self, client: AsyncClient, tokens: dict, employee_user: User, role: str, expected: int
    ) -> None:
        r = await client.post(
            "/api/v1/clock/recompute/2026-03-01",
            params={"employee_id": str(employee_user.id)},
            headers=_headers(tokens[role]),
        )
        assert r.status_code == expected, (
            f"Role {role} POST /clock/recompute/{{date}}: {r.status_code} {r.text}"
        )


class TestExportEndpoints:
    """DATEV export endpoints."""

    # POST /export/datev/preview
    @pytest.mark.parametrize(
        "role,expected",
        [
            ("geschaeftsfuehrung", 200),
            ("hr", 200),
            ("buchhaltung", 200),  # export:datev
            ("teamleiter", 403),  # LEAK: teamleiter has no export:datev
            ("mitarbeiter", 403),
            ("betriebsrat", 403),  # LEAK: BR has no export:datev
        ],
    )
    async def test_datev_preview_permissions(
        self, client: AsyncClient, tokens: dict, role: str, expected: int
    ) -> None:
        r = await client.post(
            "/api/v1/export/datev/preview",
            json={"year": 2026, "month": 3},
            headers=_headers(tokens[role]),
        )
        assert r.status_code == expected, (
            f"Role {role} POST /export/datev/preview: {r.status_code} {r.text}"
        )

    # POST /export/datev/generate
    @pytest.mark.parametrize(
        "role,expected",
        [
            ("geschaeftsfuehrung", 201),
            ("hr", 201),
            ("buchhaltung", 201),  # export:datev
            ("teamleiter", 403),
            ("mitarbeiter", 403),
            ("betriebsrat", 403),
        ],
    )
    async def test_datev_generate_permissions(
        self,
        client: AsyncClient,
        tokens: dict,
        company_id: uuid.UUID,
        role: str,
        expected: int,
    ) -> None:
        # Generate erfordert jetzt eine Firma mit Berater-/Mandanten-Nr.,
        # deren Mitarbeitern (users.company_id) der Export gilt.
        r = await client.post(
            "/api/v1/export/datev/generate",
            json={"year": 2026, "month": 3, "company_id": str(company_id)},
            headers=_headers(tokens[role]),
        )
        assert r.status_code == expected, (
            f"Role {role} POST /export/datev/generate: {r.status_code} {r.text}"
        )

    # GET /export/datev/history
    @pytest.mark.parametrize(
        "role,expected",
        [
            ("geschaeftsfuehrung", 200),
            ("hr", 200),
            ("buchhaltung", 200),
            ("teamleiter", 403),
            ("mitarbeiter", 403),
            ("betriebsrat", 403),
        ],
    )
    async def test_datev_history_permissions(
        self, client: AsyncClient, tokens: dict, role: str, expected: int
    ) -> None:
        r = await client.get("/api/v1/export/datev/history", headers=_headers(tokens[role]))
        assert r.status_code == expected, (
            f"Role {role} GET /export/datev/history: {r.status_code} {r.text}"
        )

    # GET /export/datev/{export_id}/download
    @pytest.mark.parametrize(
        "role,expected",
        [
            ("geschaeftsfuehrung", 200),
            ("hr", 200),
            ("buchhaltung", 200),
            ("teamleiter", 403),
            ("mitarbeiter", 403),
            ("betriebsrat", 403),
        ],
    )
    async def test_datev_download_permissions(
        self,
        client: AsyncClient,
        tokens: dict,
        company_id: uuid.UUID,
        export_id: uuid.UUID,
        role: str,
        expected: int,
    ) -> None:
        # Download liefert die beim Generieren gespeicherte Datei — für die
        # 200-Fälle daher zunächst via API generieren (mit Firma).
        target_id: uuid.UUID | str = export_id
        if expected == 200:
            gen = await client.post(
                "/api/v1/export/datev/generate",
                json={"year": 2026, "month": 3, "company_id": str(company_id)},
                headers=_headers(tokens[role]),
            )
            assert gen.status_code == 201, f"Role {role} generate: {gen.status_code} {gen.text}"
            target_id = gen.json()["id"]
        r = await client.get(
            f"/api/v1/export/datev/{target_id}/download",
            headers=_headers(tokens[role]),
        )
        if expected == 200 and r.status_code == 200:
            # Gespeicherte LODAS-Datei in windows-1252
            assert "windows-1252" in r.headers.get("content-type", "")
        assert r.status_code == expected, (
            f"Role {role} GET /export/datev/{{id}}/download: {r.status_code} {r.text}"
        )


class TestBrEndpoints:
    """Betriebsrat (works council) endpoints — only betriebsrat role."""

    # GET /br/overtime-stats
    @pytest.mark.parametrize(
        "role,expected",
        [
            ("geschaeftsfuehrung", 403),  # does NOT have timesheet:view_aggregates
            ("hr", 403),
            (
                "buchhaltung",
                403,
            ),  # LEAK: buchhaltung has timesheet:view_aggregates but BR endpoints must be BR-only
            ("teamleiter", 403),
            ("mitarbeiter", 403),
            ("betriebsrat", 200),
        ],
    )
    async def test_br_overtime_stats_permissions(
        self, client: AsyncClient, tokens: dict, role: str, expected: int
    ) -> None:
        r = await client.get("/api/v1/br/overtime-stats", headers=_headers(tokens[role]))
        assert r.status_code == expected, (
            f"Role {role} GET /br/overtime-stats: {r.status_code} {r.text}"
        )

    # GET /br/absence-stats
    @pytest.mark.parametrize(
        "role,expected",
        [
            ("geschaeftsfuehrung", 403),
            ("hr", 403),
            ("buchhaltung", 403),  # LEAK: buchhaltung has timesheet:view_aggregates but not BR role
            ("teamleiter", 403),
            ("mitarbeiter", 403),
            ("betriebsrat", 200),
        ],
    )
    async def test_br_absence_stats_permissions(
        self, client: AsyncClient, tokens: dict, role: str, expected: int
    ) -> None:
        r = await client.get("/api/v1/br/absence-stats", headers=_headers(tokens[role]))
        assert r.status_code == expected, (
            f"Role {role} GET /br/absence-stats: {r.status_code} {r.text}"
        )

    # GET /br/audit-trail
    @pytest.mark.parametrize(
        "role,expected",
        [
            ("geschaeftsfuehrung", 403),
            ("hr", 403),
            ("buchhaltung", 403),
            ("teamleiter", 403),
            ("mitarbeiter", 403),
            ("betriebsrat", 200),
        ],
    )
    async def test_br_audit_trail_permissions(
        self, client: AsyncClient, tokens: dict, role: str, expected: int
    ) -> None:
        r = await client.get("/api/v1/br/audit-trail", headers=_headers(tokens[role]))
        assert r.status_code == expected, (
            f"Role {role} GET /br/audit-trail: {r.status_code} {r.text}"
        )


class TestUnauthenticatedAccess:
    """Every protected endpoint must return 401 without a token."""

    @pytest.mark.parametrize(
        "method,path",
        [
            ("GET", "/api/v1/users"),
            ("POST", "/api/v1/users"),
            ("GET", "/api/v1/users/me"),
            ("GET", "/api/v1/users/me/export"),
            ("GET", "/api/v1/auth/me"),
            # /auth/logout deliberately does NOT require auth (fkw.4): the
            # refresh cookie IS the credential, an expired access token must
            # not block logout. Covered separately in TestAuthEndpoints.
            ("GET", "/api/v1/admin/dashboard"),
            ("GET", "/api/v1/admin/flagged"),
            ("GET", "/api/v1/admin/pending-approvals"),
            ("POST", "/api/v1/admin/bulk-approve"),
            ("GET", "/api/v1/admin/monthly-report"),
            ("GET", "/api/v1/admin/audit-log"),
            ("POST", "/api/v1/absences/requests"),
            ("GET", "/api/v1/absences/requests"),
            ("GET", "/api/v1/absences/balance"),
            ("GET", "/api/v1/absences/calendar"),
            ("POST", "/api/v1/clock/punch"),
            ("GET", "/api/v1/clock/status"),
            ("GET", "/api/v1/clock/presence"),
            ("GET", "/api/v1/clock/timesheet"),
            ("POST", "/api/v1/export/datev/preview"),
            ("POST", "/api/v1/export/datev/generate"),
            ("GET", "/api/v1/export/datev/history"),
            ("GET", "/api/v1/br/overtime-stats"),
            ("GET", "/api/v1/br/absence-stats"),
            ("GET", "/api/v1/br/audit-trail"),
        ],
    )
    async def test_no_token_returns_401(self, client: AsyncClient, method: str, path: str) -> None:
        r = await client.request(method, path)
        assert r.status_code == 401, (
            f"{method} {path} without token: expected 401, got {r.status_code}"
        )
