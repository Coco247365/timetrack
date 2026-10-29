"""Tests for the live presence overview endpoint (GET /clock/presence).

Verifies scope-based data visibility, response shape, and status derivation
from punch events.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from zeiterfassung.core.security import create_access_token
from zeiterfassung.models.clock import EventSource, EventType, PunchEvent
from zeiterfassung.models.department import Department
from zeiterfassung.models.tenant import Tenant
from zeiterfassung.models.user import User

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
        name="Presence Test GmbH",
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
    user_specs = [
        ("Max", "Müller", "max@test.de", "Lager"),
        ("Anna", "Schmidt", "anna@test.de", "Lager"),
        ("Klaus", "Fischer", "klaus@test.de", "Lager"),
        ("Peter", "Becker", "peter@test.de", "Verkauf"),
        ("Maria", "Hoffmann", "maria@test.de", "Verkauf"),
    ]
    result = {}
    for i, (first, last, email, dept) in enumerate(user_specs, start=1):
        u = User(
            tenant_id=tenant.id,
            email=email,
            hashed_password=pw,
            first_name=first,
            last_name=last,
            employee_number=f"EMP{i:03d}",
            contracted_hours_per_week=40.0,
            is_active=True,
            department_id=departments[dept].id,
            created_at=now,
            updated_at=now,
        )
        db.add(u)
        result[email] = u
    await db.flush()
    return result


@pytest_asyncio.fixture
async def punch_events(db: AsyncSession, tenant: Tenant, users: dict[str, User]) -> None:
    """Seed punch events: 3 clocked in, 1 on break, 1 clocked out."""
    now = datetime.now(tz=UTC)
    events = [
        # Max: clocked in (Lager)
        (users["max@test.de"].id, EventType.PUNCH_IN, now.replace(hour=7, minute=0)),
        # Anna: on break (Lager)
        (users["anna@test.de"].id, EventType.PUNCH_IN, now.replace(hour=6, minute=30)),
        (users["anna@test.de"].id, EventType.BREAK_START, now.replace(hour=12, minute=0)),
        # Klaus: clocked out (Lager)
        (users["klaus@test.de"].id, EventType.PUNCH_IN, now.replace(hour=7, minute=0)),
        (users["klaus@test.de"].id, EventType.PUNCH_OUT, now.replace(hour=15, minute=30)),
        # Peter: clocked in (Verkauf)
        (users["peter@test.de"].id, EventType.PUNCH_IN, now.replace(hour=8, minute=0)),
        # Maria: clocked in after break (Verkauf)
        (users["maria@test.de"].id, EventType.PUNCH_IN, now.replace(hour=8, minute=15)),
        (users["maria@test.de"].id, EventType.BREAK_START, now.replace(hour=12, minute=30)),
        (users["maria@test.de"].id, EventType.BREAK_END, now.replace(hour=13, minute=0)),
    ]
    for emp_id, etype, etime in events:
        db.add(
            PunchEvent(
                tenant_id=tenant.id,
                employee_id=emp_id,
                event_type=etype,
                event_time=etime,
                source=EventSource.WEB_PORTAL,
                created_at=now,
            )
        )
    await db.flush()


# ===========================================================================
# Tests
# ===========================================================================


class TestPresenceGFAccess:
    """GF (global scope) sees all employees across departments."""

    async def test_gf_gets_all_employees(
        self,
        client: AsyncClient,
        db: AsyncSession,
        tenant: Tenant,
        users: dict,
        punch_events,
        departments,
    ):
        token = await _make_token(
            db,
            tenant.id,
            users["max@test.de"].id,
            permissions=["timesheet:view_all", "timesheet:view_own"],
            data_scope="global",
            role_type="gf",
        )
        r = await client.get("/api/v1/clock/presence", headers=_headers(token))
        assert r.status_code == 200
        data = r.json()
        assert data["total_employees"] == 5
        assert data["present_count"] == 4  # Max, Anna (break), Peter, Maria
        assert data["on_break_count"] == 1  # Anna
        assert len(data["employees"]) == 5

    async def test_response_shape(
        self,
        client: AsyncClient,
        db: AsyncSession,
        tenant: Tenant,
        users: dict,
        punch_events,
        departments,
    ):
        token = await _make_token(
            db,
            tenant.id,
            users["max@test.de"].id,
            permissions=["timesheet:view_all"],
            data_scope="global",
            role_type="gf",
        )
        r = await client.get("/api/v1/clock/presence", headers=_headers(token))
        data = r.json()
        emp = data["employees"][0]
        assert "employee_id" in emp
        assert "first_name" in emp
        assert "last_name" in emp
        assert "department_id" in emp
        assert "department_name" in emp
        assert "status" in emp
        assert emp["status"] in ("clocked_in", "on_break", "clocked_out")
        assert "today_work_minutes" in emp

    async def test_gf_sees_both_departments(
        self,
        client: AsyncClient,
        db: AsyncSession,
        tenant: Tenant,
        users: dict,
        punch_events,
        departments,
    ):
        token = await _make_token(
            db,
            tenant.id,
            users["max@test.de"].id,
            permissions=["timesheet:view_all"],
            data_scope="global",
            role_type="gf",
        )
        r = await client.get("/api/v1/clock/presence", headers=_headers(token))
        dept_names = {e["department_name"] for e in r.json()["employees"]}
        assert "Lager" in dept_names
        assert "Verkauf" in dept_names


class TestPresenceTeamleiterScope:
    """Teamleiter sees only their department's employees."""

    async def test_teamleiter_sees_only_own_department(
        self,
        client: AsyncClient,
        db: AsyncSession,
        tenant: Tenant,
        users: dict,
        punch_events,
        departments,
    ):
        token = await _make_token(
            db,
            tenant.id,
            users["max@test.de"].id,
            permissions=["timesheet:view_team", "timesheet:view_own"],
            data_scope="department",
            role_type="teamleiter",
            department_id=departments["Lager"].id,
        )
        r = await client.get("/api/v1/clock/presence", headers=_headers(token))
        assert r.status_code == 200
        data = r.json()
        assert data["total_employees"] == 3  # Max, Anna, Klaus (all Lager)
        dept_names = {e["department_name"] for e in data["employees"]}
        assert dept_names == {"Lager"}

    async def test_teamleiter_cannot_see_other_department(
        self,
        client: AsyncClient,
        db: AsyncSession,
        tenant: Tenant,
        users: dict,
        punch_events,
        departments,
    ):
        token = await _make_token(
            db,
            tenant.id,
            users["max@test.de"].id,
            permissions=["timesheet:view_team", "timesheet:view_own"],
            data_scope="department",
            role_type="teamleiter",
            department_id=departments["Lager"].id,
        )
        r = await client.get("/api/v1/clock/presence", headers=_headers(token))
        emails = {e["first_name"] for e in r.json()["employees"]}
        assert "Peter" not in emails  # Verkauf
        assert "Maria" not in emails  # Verkauf


class TestPresenceStatusDerivation:
    """Verify status mapping from punch events."""

    async def test_clocked_in_status(
        self,
        client: AsyncClient,
        db: AsyncSession,
        tenant: Tenant,
        users: dict,
        punch_events,
        departments,
    ):
        token = await _make_token(
            db,
            tenant.id,
            users["max@test.de"].id,
            permissions=["timesheet:view_all"],
            data_scope="global",
            role_type="gf",
        )
        r = await client.get("/api/v1/clock/presence", headers=_headers(token))
        emps = {e["first_name"]: e for e in r.json()["employees"]}

        assert emps["Max"]["status"] == "clocked_in"
        assert emps["Anna"]["status"] == "on_break"
        assert emps["Klaus"]["status"] == "clocked_out"
        assert emps["Peter"]["status"] == "clocked_in"
        assert emps["Maria"]["status"] == "clocked_in"  # break ended, back to work


class TestPresenceDenied:
    """Roles without view_all/view_team cannot access presence."""

    @pytest.mark.parametrize(
        "permissions,data_scope,role_type",
        [
            (
                ["timesheet:view_own", "leave:request_own", "users:read_own", "export:own_data"],
                "self",
                "mitarbeiter",
            ),
            (
                ["timesheet:view_aggregates", "admin:dashboard", "export:datev"],
                "self",
                "buchhaltung",
            ),
            (
                ["timesheet:view_aggregates", "audit:read_anonymized"],
                "anonymized_global",
                "betriebsrat",
            ),
        ],
    )
    async def test_denied_roles(
        self,
        client: AsyncClient,
        db: AsyncSession,
        tenant: Tenant,
        users: dict,
        permissions: list[str],
        data_scope: str,
        role_type: str,
    ):
        token = await _make_token(
            db,
            tenant.id,
            users["max@test.de"].id,
            permissions=permissions,
            data_scope=data_scope,
            role_type=role_type,
        )
        r = await client.get("/api/v1/clock/presence", headers=_headers(token))
        assert r.status_code == 403


class TestPresenceNoToken:
    """Unauthenticated access returns 401."""

    async def test_no_token(self, client: AsyncClient):
        r = await client.get("/api/v1/clock/presence")
        assert r.status_code == 401


class TestOrphanedBreakAfterOut:
    """Verwaister Pause-Beginn NACH einem (nachgetragenen) Gehen ≠ anwesend.

    Realfall: Kommen am Vortag, Gehen vergessen; Terminal-Tap am Folgetag wird
    als Pause gebucht (Schicht galt noch als offen); danach trägt der Admin das
    Gehen für den Vortag nach. Das letzte Event ist dann ein Pause-Beginn nach
    dem Gehen — die Liste zeigte fälschlich "Anwesend seit <Vortag>".
    """

    async def test_orphaned_break_shows_clocked_out(
        self,
        client: AsyncClient,
        db: AsyncSession,
        tenant: Tenant,
        users: dict,
        departments,
    ):
        from datetime import timedelta

        now = datetime.now(tz=UTC)
        yesterday = now - timedelta(days=1)
        max_id = users["max@test.de"].id
        events = [
            (EventType.PUNCH_IN, yesterday.replace(hour=11, minute=8)),
            (EventType.PUNCH_OUT, yesterday.replace(hour=15, minute=0)),  # nachgetragen
            (EventType.BREAK_START, now.replace(hour=7, minute=5)),  # verwaist
        ]
        for etype, etime in events:
            db.add(
                PunchEvent(
                    tenant_id=tenant.id,
                    employee_id=max_id,
                    event_type=etype,
                    event_time=etime,
                    source=EventSource.NFC_TERMINAL,
                    created_at=now,
                )
            )
        await db.flush()

        token = await _make_token(
            db,
            tenant.id,
            max_id,
            permissions=["timesheet:view_all"],
            data_scope="global",
            role_type="gf",
        )
        r = await client.get("/api/v1/clock/presence", headers=_headers(token))
        emps = {e["first_name"]: e for e in r.json()["employees"]}

        assert emps["Max"]["status"] == "clocked_out"
        assert emps["Max"]["clocked_in_since"] is None


class TestAutoPresence:
    """Auto-Erfasste gelten als anwesend, solange "jetzt" in den hinterlegten
    Arbeitszeiten liegt — außer bei Abwesenheit (Urlaub/Krank/...).

    Aufrufe gehen direkt gegen den Service mit injiziertem ``now``, damit die
    Tests systemuhr-unabhängig sind. Fixtag: Mi 12.08.2026 (kein Feiertag HH).
    """

    NOW_IN = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)  # 12:00 lokal
    NOW_OUT = datetime(2026, 8, 12, 18, 0, tzinfo=UTC)  # 20:00 lokal

    def _scope(self, tenant_id):
        from zeiterfassung.api.deps import ScopeContext

        return ScopeContext(scope="global", tenant_id=tenant_id, user_id=None)

    async def _mk_auto_user(self, db, tenant, departments):
        from zeiterfassung.core.security import hash_password

        now = datetime.now(tz=UTC)
        u = User(
            tenant_id=tenant.id,
            email="auto@test.de",
            hashed_password=hash_password("test123"),
            first_name="Auto",
            last_name="Erfasst",
            employee_number="EMP900",
            contracted_hours_per_week=40.0,
            time_capture="auto",
            is_active=True,
            department_id=departments["Lager"].id,
            created_at=now,
            updated_at=now,
        )
        db.add(u)
        await db.flush()
        return u

    async def _status_of(self, db, tenant, uid, now):
        from zeiterfassung.services.clock_service import get_presence_overview

        data = await get_presence_overview(db, tenant.id, self._scope(tenant.id), now=now)
        return {e["employee_id"]: e for e in data["employees"]}[uid]

    async def test_im_zeitfenster_anwesend_ohne_tagesbogen(self, db, tenant, departments, users):
        # Kein Tagesbogen für heute → Standardfenster 07:00 + Soll + Pause
        # (wie ensure_auto_timesheets): 12:00 lokal liegt darin.
        u = await self._mk_auto_user(db, tenant, departments)
        e = await self._status_of(db, tenant, u.id, self.NOW_IN)
        assert e["status"] == "clocked_in"
        assert e["clocked_in_since"] is not None

    async def test_ausserhalb_zeitfenster_abwesend(self, db, tenant, departments, users):
        u = await self._mk_auto_user(db, tenant, departments)
        e = await self._status_of(db, tenant, u.id, self.NOW_OUT)
        assert e["status"] == "clocked_out"

    async def test_nach_feierabend_steht_die_arbeitszeit_als_von_bis(
        self, db, tenant, departments, users
    ):
        """Fenster vorbei: abwesend, aber MIT der hinterlegten Arbeitszeit.

        Sonst stünde bei Auto-Erfassten ab Feierabend wieder „–" in der Liste,
        obwohl sie den ganzen Tag gearbeitet haben.
        """
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("Europe/Berlin")
        u = await self._mk_auto_user(db, tenant, departments)
        e = await self._status_of(db, tenant, u.id, self.NOW_OUT)
        assert e["status"] == "clocked_out"
        assert e["today_start"].astimezone(tz).strftime("%H:%M") == "07:00"
        # 40 h/Woche = 480 min Soll + 30 min Pflichtpause -> 15:30 lokal.
        assert e["today_end"].astimezone(tz).strftime("%H:%M") == "15:30"

    async def test_vor_arbeitsbeginn_keine_zeiten(self, db, tenant, departments, users):
        """Frueh am Morgen ist noch nichts passiert — kein „von … bis …"."""
        u = await self._mk_auto_user(db, tenant, departments)
        e = await self._status_of(db, tenant, u.id, datetime(2026, 8, 12, 3, 0, tzinfo=UTC))
        assert e["status"] == "clocked_out"
        assert e["today_start"] is None and e["today_end"] is None

    async def test_tagesbogen_fenster_gewinnt(self, db, tenant, departments, users):
        # Manuell angepasster Auto-Tag 16:00–20:00 lokal → um 12:00 lokal NICHT
        # anwesend, um 20:00 lokal (18:00 UTC) schon.
        from zeiterfassung.models.clock import DailyTimesheet, TimesheetStatus

        u = await self._mk_auto_user(db, tenant, departments)
        now = datetime.now(tz=UTC)
        db.add(
            DailyTimesheet(
                tenant_id=tenant.id,
                employee_id=u.id,
                work_date=date(2026, 8, 12),
                start_time=datetime(2026, 8, 12, 14, 0, tzinfo=UTC),
                end_time=datetime(2026, 8, 12, 18, 30, tzinfo=UTC),
                break_minutes=30,
                net_work_minutes=240,
                gross_work_minutes=270,
                overtime_minutes=0,
                status=TimesheetStatus.COMPLETE,
                computed_from_events=False,
                created_at=now,
                updated_at=now,
            )
        )
        await db.flush()
        assert (await self._status_of(db, tenant, u.id, self.NOW_IN))["status"] == "clocked_out"
        assert (await self._status_of(db, tenant, u.id, self.NOW_OUT))["status"] == "clocked_in"

    async def test_urlaub_gewinnt_gegen_auto_anwesenheit(self, db, tenant, departments, users):
        from decimal import Decimal

        from zeiterfassung.models.absence import (
            ApprovalRequest,
            DayPart,
            RequestStatus,
            RequestType,
        )

        u = await self._mk_auto_user(db, tenant, departments)
        now = datetime.now(tz=UTC)
        db.add(
            ApprovalRequest(
                tenant_id=tenant.id,
                employee_id=u.id,
                request_type=RequestType.VACATION,
                status=RequestStatus.APPROVED,
                date_from=date(2026, 8, 12),
                date_to=date(2026, 8, 12),
                day_part=DayPart.FULL,
                days_count=Decimal("1"),
                created_at=now,
                updated_at=now,
            )
        )
        await db.flush()
        e = await self._status_of(db, tenant, u.id, self.NOW_IN)
        assert e["status"] == "vacation"

    async def test_nicht_gearbeitet_marker_bleibt_abwesend(self, db, tenant, departments, users):
        # Leerer Tagesbogen (net=0, keine Zeiten) = "nicht gearbeitet" → kein
        # Standardfenster-Fallback, Tag bleibt abwesend.
        from zeiterfassung.models.clock import DailyTimesheet, TimesheetStatus

        u = await self._mk_auto_user(db, tenant, departments)
        now = datetime.now(tz=UTC)
        db.add(
            DailyTimesheet(
                tenant_id=tenant.id,
                employee_id=u.id,
                work_date=date(2026, 8, 12),
                start_time=None,
                end_time=None,
                break_minutes=0,
                net_work_minutes=0,
                gross_work_minutes=0,
                overtime_minutes=0,
                status=TimesheetStatus.COMPLETE,
                computed_from_events=False,
                created_at=now,
                updated_at=now,
            )
        )
        await db.flush()
        e = await self._status_of(db, tenant, u.id, self.NOW_IN)
        assert e["status"] == "clocked_out"

    async def test_gestempelte_bleiben_unveraendert(self, db, tenant, departments, users):
        # NFC-Stempler ohne Events heute bleibt abwesend — kein Auto-Fenster.
        e = await self._status_of(db, tenant, users["max@test.de"].id, self.NOW_IN)
        assert e["status"] == "clocked_out"
