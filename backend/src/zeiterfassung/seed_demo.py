"""Demo seed script for the Zeiterfassung MVP.

Populates realistic 3-year test data (March 2023 – March 2026) for
Muster GmbH.

Usage:
    cd backend
    ZEITERFASSUNG_DATABASE_URL="postgresql+asyncpg://tikiton:tikiton@localhost:5434/zeiterfassung" \\
        uv run python -m zeiterfassung.seed_demo
"""

from __future__ import annotations

import asyncio
import os
import random
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from zeiterfassung.config import get_settings
from zeiterfassung.core.holidays import get_holidays, is_holiday, is_weekend
from zeiterfassung.core.security import hash_password
from zeiterfassung.models import (
    ActorType,
    ApprovalAuditLog,
    ApprovalRequest,
    DailyTimesheet,
    Department,
    EventSource,
    EventType,
    MonthlySummary,
    PunchEvent,
    RequestStatus,
    RequestType,
    Role,
    Tenant,
    TimesheetStatus,
    User,
    UserRole,
)
from zeiterfassung.services.clock_service import recompute_daily_timesheet

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BUNDESLAND = "HH"
# Demo "today": the real current date, so the demo always looks current.
# Set DEMO_TODAY=YYYY-MM-DD for a reproducible data set.
TODAY = (
    date.fromisoformat(os.environ["DEMO_TODAY"]) if os.environ.get("DEMO_TODAY") else date.today()
)
HISTORY_START = date(TODAY.year - 3, TODAY.month, 1)
HISTORY_END = TODAY - timedelta(days=1)  # day before today (last full day)

# Contracted daily minutes (based on 40h/week → 480 min/day, 30h → 360, 20h → 240)
_CONTRACTED_DAILY_BY_HOURS: dict[int, int] = {
    40: 480,
    30: 360,
    20: 240,
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def utc_dt(d: date, hour: int, minute: int = 0) -> datetime:
    """Return a UTC-aware datetime for the given date and time.

    The demo company is in Germany (UTC+1 in winter, UTC+2 in summer). For demo data we
    use UTC directly; durations are correct and the demo purpose is met.
    """
    return datetime(d.year, d.month, d.day, hour, minute, 0, tzinfo=UTC)


def working_days_back(from_date: date, n: int) -> list[date]:
    """Return the last *n* working days (Mon-Fri, no public holidays) before *from_date* inclusive."""
    days: list[date] = []
    cursor = from_date
    while len(days) < n:
        if not is_weekend(cursor) and not is_holiday(cursor, BUNDESLAND):
            days.append(cursor)
        cursor -= timedelta(days=1)
    return sorted(days)


def iter_working_days(start: date, end: date) -> list[date]:
    """Return all working days (Mon–Fri, no HH holidays) between start and end inclusive."""
    days: list[date] = []
    cursor = start
    while cursor <= end:
        if not is_weekend(cursor) and not is_holiday(cursor, BUNDESLAND):
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def iter_months(start: date, end: date) -> list[date]:
    """Yield first-of-month dates from start's month to end's month inclusive."""
    months: list[date] = []
    cur = date(start.year, start.month, 1)
    stop = date(end.year, end.month, 1)
    while cur <= stop:
        months.append(cur)
        # Advance to next month
        if cur.month == 12:
            cur = date(cur.year + 1, 1, 1)
        else:
            cur = date(cur.year, cur.month + 1, 1)
    return months


# ---------------------------------------------------------------------------
# Clear
# ---------------------------------------------------------------------------

# Deletion order respects FK constraints (most-dependent first).
_CLEAR_ORDER = [
    "audit_log",
    "approval_audit_log",
    "timesheet_corrections",
    "payroll_exports",
    "wage_type_mappings",
    "monthly_summaries",
    "daily_timesheets",
    "punch_events",
    "approval_requests",
    "approval_delegations",
    "user_roles",
    "users",
    "departments",
    "roles",
    "tenants",
]


async def clear_all(session: AsyncSession) -> None:
    print("  Clearing existing data...")
    for table in _CLEAR_ORDER:
        await session.execute(text(f"DELETE FROM {table}"))  # noqa: S608
    await session.commit()
    print("  Done.")


# ---------------------------------------------------------------------------
# Tenant
# ---------------------------------------------------------------------------


async def create_tenant(session: AsyncSession) -> Tenant:
    tenant = Tenant(
        id=uuid.uuid4(),
        name="Muster GmbH",
        timezone="Europe/Berlin",
        locale="de-DE",
        bundesland="HH",
        is_active=True,
        settings={
            "company_address": "Musterstraße 1, 12345 Musterstadt",
            "datev_mandant": "12345",
            "vacation_days_per_year": 30,
            "overtime_threshold_daily_h": 8,
        },
    )
    session.add(tenant)
    await session.flush()
    print(f"  Tenant: {tenant.name} ({tenant.id})")
    return tenant


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------

GF_PERMS = [
    "timesheet:view_all",
    "timesheet:edit_all",
    "timesheet:view_own",
    "leave:approve_all",
    "leave:request_own",
    "users:read_all",
    "users:read_own",
    "users:write",
    "export:datev",
    "export:own_data",
    "admin:dashboard",
    "admin:settings",
    "admin:features",
    "audit:read",
]
HR_PERMS = [
    "timesheet:view_all",
    "timesheet:edit_all",
    "timesheet:view_own",
    "leave:approve_all",
    "leave:request_own",
    "users:read_all",
    "users:read_own",
    "users:write",
    "export:datev",
    "export:own_data",
    "admin:dashboard",
    "audit:read",
    "medical:read",
]
BUCHHALTUNG_PERMS = [
    "timesheet:view_own",
    "timesheet:view_aggregates",
    "leave:request_own",
    "users:read_own",
    "export:datev",
    "export:own_data",
    "admin:dashboard",
]
TEAMLEITER_PERMS = [
    "timesheet:view_team",
    "timesheet:edit_team",
    "timesheet:view_own",
    "leave:approve_team",
    "leave:request_own",
    "users:read_team",
    "users:read_own",
    "export:own_data",
]
EMPLOYEE_PERMS = [
    "timesheet:view_own",
    "leave:request_own",
    "users:read_own",
    "export:own_data",
]
BR_PERMS = [
    "timesheet:view_own",
    "timesheet:view_aggregates",
    "leave:request_own",
    "users:read_own",
    "export:own_data",
    "audit:read_anonymized",
]


async def create_roles(session: AsyncSession, tenant: Tenant) -> dict[str, Role]:
    roles: dict[str, Role] = {}
    # (name, role_type, data_scope, permissions)
    specs = [
        ("Geschäftsführung", "gf", "global", GF_PERMS),
        ("Personalleitung", "hr", "global", HR_PERMS),
        ("Buchhaltung", "buchhaltung", "global", BUCHHALTUNG_PERMS),
        ("Teamleiter", "teamleiter", "department", TEAMLEITER_PERMS),
        ("Mitarbeiter", "mitarbeiter", "self", EMPLOYEE_PERMS),
        ("Betriebsrat", "betriebsrat", "anonymized_global", BR_PERMS),
    ]
    for name, role_type, data_scope, perms in specs:
        role = Role(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            name=name,
            role_type=role_type,
            data_scope=data_scope,
            permissions=perms,
        )
        session.add(role)
        roles[name] = role
    await session.flush()
    print(f"  Roles: {', '.join(roles)}")
    return roles


# ---------------------------------------------------------------------------
# Departments
# ---------------------------------------------------------------------------

DEMO_DEPARTMENTS = [
    ("Geschäftsführung", "GF"),
    ("IT", "IT"),
    ("Lager", "LAG"),
    ("Verkauf", "VKF"),
    ("Logistik", "LOG"),
    ("Buchhaltung", "BUC"),
    ("Verwaltung", "VWL"),
    ("Personalleitung", "HR"),
    ("Betriebsrat", "BR"),
    ("Werkstatt", "WKS"),
]


async def create_departments(
    session: AsyncSession,
    tenant: Tenant,
) -> dict[str, Department]:
    """Create all demo departments."""
    depts: dict[str, Department] = {}
    for name, code in DEMO_DEPARTMENTS:
        dept = Department(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            name=name,
            code=code,
            is_active=True,
        )
        session.add(dept)
        depts[name] = dept
    await session.flush()
    print(f"  Departments: {', '.join(name for name, _ in DEMO_DEPARTMENTS)}")
    return depts


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

# fmt: off
_USER_SPECS = [
    # (first, last,          email,                           dept,              role_key,           hours, badge,    hire_date)
    # --- Management / admin ---
    ("Lena",     "Hoffmann",   "l.hoffmann@example.com",         "IT",              "Geschäftsführung", 40, None,     None),
    ("Jan",      "Vogel",      "j.vogel@example.com",       "Geschäftsführung","Geschäftsführung", 40, None,     None),
    ("Hubertus", "Schneider",  "h.schneider@example.com",         "Personalleitung", "Personalleitung",  40, None,     None),
    # --- Teamleiter ---
    ("Thomas",   "Weber",      "t.weber@example.com",        "Lager",           "Teamleiter",       40, "NFC003", None),
    ("Stefan",   "Müller",     "s.mueller@example.com",      "Verkauf",         "Teamleiter",       40, "NFC004", None),
    # --- Lager (8 employees) ---
    ("Max",      "Mustermann", "m.mustermann@example.com",   "Lager",           "Mitarbeiter",      40, "NFC005", None),
    ("Erika",    "Schmidt",    "e.schmidt@example.com",      "Lager",           "Mitarbeiter",      40, "NFC006", None),
    ("Klaus",    "Fischer",    "k.fischer@example.com",      "Lager",           "Mitarbeiter",      40, "NFC007", None),
    ("Anna",     "Wagner",     "a.wagner@example.com",       "Lager",           "Mitarbeiter",      30, "NFC008", None),
    ("Jürgen",   "Lange",      "j.lange@example.com",        "Lager",           "Mitarbeiter",      40, "NFC015", date(2024, 1, 1)),
    ("Heinrich", "Brandt",     "h.brandt@example.com",       "Lager",           "Mitarbeiter",      40, "NFC016", None),
    ("Markus",   "Hartmann",   "m.hartmann@example.com",     "Lager",           "Mitarbeiter",      40, "NFC017", date(2023, 6, 1)),
    ("Petra",    "Krause",     "p.krause@example.com",       "Lager",           "Mitarbeiter",      30, "NFC018", date(2023, 9, 1)),
    # --- Logistik (3 employees) ---
    ("Wolfgang", "Schulz",     "w.schulz@example.com",       "Logistik",        "Mitarbeiter",      40, "NFC011", None),
    ("Frank",    "Richter",    "f.richter@example.com",      "Logistik",        "Mitarbeiter",      40, "NFC013", None),
    ("Dieter",   "Meier",      "d.meier@example.com",        "Logistik",        "Mitarbeiter",      40, "NFC019", date(2024, 3, 1)),
    # --- Verkauf (3 employees) ---
    ("Peter",    "Becker",     "p.becker@example.com",       "Verkauf",         "Mitarbeiter",      40, "NFC009", None),
    ("Maria",    "Hoffmann",   "m.hoffmann@example.com",     "Verkauf",         "Mitarbeiter",      40, "NFC010", None),
    ("Sandra",   "Neumann",    "s.neumann@example.com",      "Verkauf",         "Mitarbeiter",      30, "NFC020", date(2023, 11, 1)),
    # --- Verwaltung (2 employees) ---
    ("Claudia",  "Braun",      "c.braun@example.com",        "Verwaltung",      "Mitarbeiter",      20, "NFC014", date(2024, 9, 1)),
    ("Renate",   "Klein",      "r.klein@example.com",        "Verwaltung",      "Mitarbeiter",      30, "NFC021", date(2023, 4, 1)),
    # --- Buchhaltung (2 employees) ---
    ("Sabine",   "Koch",       "s.koch@example.com",         "Buchhaltung",     "Buchhaltung",      30, "NFC012", None),
    ("Ursula",   "Wolf",       "u.wolf@example.com",         "Buchhaltung",     "Buchhaltung",      40, "NFC022", None),
    # --- IT (1 employee, Lena Hoffmann is GF/IT) ---
    ("Tobias",   "Zimmermann", "t.zimmermann@example.com",   "IT",              "Mitarbeiter",      40, None,     date(2024, 6, 1)),
    # --- Personalleitung (2 employees) ---
    ("Karin",    "Schwarz",    "k.schwarz@example.com",      "Personalleitung", "Personalleitung",  40, None,     None),
    ("Gerd",     "Krüger",     "g.krueger@example.com",      "Personalleitung", "Mitarbeiter",      40, None,     date(2023, 7, 1)),
    # --- Werkstatt (2 employees) ---
    ("Hans",     "Schäfer",    "h.schaefer@example.com",     "Werkstatt",       "Mitarbeiter",      40, "NFC023", None),
    ("Fritz",    "Bauer",      "f.bauer@example.com",        "Werkstatt",       "Mitarbeiter",      40, "NFC024", date(2024, 2, 1)),
    # --- Dedicated demo accounts (role name = password) ---
    ("GF",              "",              "gf@example.com",          "Geschäftsführung","Geschäftsführung", 40, None,     None),
    ("HR",              "",              "hr@example.com",          "Personalleitung", "Personalleitung",  40, None,     None),
    ("TL",              "Lager",         "teamleiter@example.com",  "Lager",           "Teamleiter",       40, None,     None),
    ("TL",              "Verkauf",       "teamleiter2@example.com", "Verkauf",         "Teamleiter",       40, None,     None),
    ("MA",              "Lager",         "mitarbeiter@example.com", "Lager",           "Mitarbeiter",      40, "NFC099", None),
    ("MA",              "Verkauf",       "mitarbeiter2@example.com","Verkauf",         "Mitarbeiter",      40, "NFC098", None),
    ("Buchhaltung",     "",              "buchhaltung@example.com", "Buchhaltung",     "Buchhaltung",      40, None,     None),
    ("Betriebsrat",     "",              "betriebsrat@example.com", "Betriebsrat",     "Betriebsrat",      40, None,     None),
]
# fmt: on

# Departments treated as "warehouse" get earlier start times.
_WAREHOUSE_DEPTS = {"Lager", "Logistik"}


def _is_warehouse(dept: str) -> bool:
    return dept in _WAREHOUSE_DEPTS


# Employees who consistently accumulate overtime
_OVERTIME_PRONE = {"k.fischer@example.com", "f.richter@example.com"}

# Employee with Langzeitkrankheit
_LANGZEIT_EMPLOYEE = "w.schulz@example.com"
_LANGZEIT_START = date(2024, 6, 3)
_LANGZEIT_END = date(2024, 7, 12)  # ~6 weeks

# Special leave events: (email, date_from, date_to, reason_code, reason_text)
_SPECIAL_LEAVES = [
    ("s.koch@example.com", date(2024, 6, 17), date(2024, 6, 18), "UMZUG", "Wohnungsumzug"),
    ("s.mueller@example.com", date(2024, 10, 7), date(2024, 10, 7), "GEBURT", "Geburt Kind"),
    (
        "p.becker@example.com",
        (TODAY + timedelta(days=-309)),
        (TODAY + timedelta(days=-308)),
        "HOCHZEIT",
        "Hochzeit",
    ),
]


async def create_users(
    session: AsyncSession,
    tenant: Tenant,
    roles: dict[str, Role],
    departments: dict[str, Department],
) -> dict[str, User]:
    """Create all demo users and their role assignments."""
    users: dict[str, User] = {}  # keyed by email

    # Password mapping: demo.* accounts use role name, others use admin123/demo123
    _PRIVILEGED_ROLES = {"Geschäftsführung", "Personalleitung", "Buchhaltung"}
    _ROLE_PASSWORDS = {
        "gf@example.com": "geschaeftsfuehrung",
        "hr@example.com": "personalleitung",
        "teamleiter@example.com": "teamleiter",
        "teamleiter2@example.com": "teamleiter",
        "mitarbeiter@example.com": "mitarbeiter",
        "mitarbeiter2@example.com": "mitarbeiter",
        "buchhaltung@example.com": "buchhaltung",
        "betriebsrat@example.com": "betriebsrat",
    }
    admin_hash = hash_password("admin123")
    employee_hash = hash_password("demo123")
    # Pre-hash role passwords
    role_pw_hashes = {email: hash_password(pw) for email, pw in _ROLE_PASSWORDS.items()}

    for idx, (first, last, email, dept, role_key, hours, badge, _hire) in enumerate(
        _USER_SPECS, start=1
    ):
        if email in role_pw_hashes:
            pw_hash = role_pw_hashes[email]
        elif role_key in _PRIVILEGED_ROLES:
            pw_hash = admin_hash
        else:
            pw_hash = employee_hash
        dept_id = departments[dept].id if dept in departments else None
        user = User(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            email=email,
            hashed_password=pw_hash,
            first_name=first,
            last_name=last,
            employee_number=f"{1000 + idx}",
            nfc_badge_id=badge,
            is_active=True,
            contracted_hours_per_week=float(hours),
            department_id=dept_id,
        )
        session.add(user)
        users[email] = user

        user_role = UserRole(user_id=user.id, role_id=roles[role_key].id)
        session.add(user_role)

    await session.flush()
    print(f"  Users: {len(users)} created")
    return users


def _user_by_name(users: dict[str, User], email: str) -> User:
    return users[email]


# ---------------------------------------------------------------------------
# Absence date-range helpers
# ---------------------------------------------------------------------------


def _working_days_in_range(d_from: date, d_to: date) -> list[date]:
    """All working days between d_from and d_to inclusive."""
    return iter_working_days(d_from, d_to)


def _count_working_days(d_from: date, d_to: date) -> int:
    return len(_working_days_in_range(d_from, d_to))


# ---------------------------------------------------------------------------
# Build absence calendars (sick days, vacation days, special leave)
# so we can skip punch events for those days.
# ---------------------------------------------------------------------------


def _build_absence_calendar(
    users: dict[str, User],
) -> dict[str, set[date]]:
    """Return a dict mapping email -> set of dates the employee is absent (sick/vacation/special).

    This is computed deterministically before punch event generation so we can
    skip those days.
    """
    absence_days: dict[str, set[date]] = {email: set() for email in users}

    # ---- Build per-employee specs lookup ----
    user_specs_by_email: dict[str, tuple] = {
        email: (first, last, email, dept, role_key, hours, badge, hire)
        for (first, last, email, dept, role_key, hours, badge, hire) in _USER_SPECS
    }

    for email in users:
        spec = user_specs_by_email[email]
        hire_date: date | None = spec[7]
        emp_start = max(HISTORY_START, hire_date) if hire_date else HISTORY_START
        emp_rng = random.Random(f"absence-{email}")

        # --- Langzeitkrankheit: Wolfgang Schulz ---
        if email == _LANGZEIT_EMPLOYEE:
            for d in _working_days_in_range(_LANGZEIT_START, _LANGZEIT_END):
                absence_days[email].add(d)

        # --- Generate sick episodes per year ---
        for year in range(emp_start.year, TODAY.year + 1):
            year_start = max(emp_start, date(year, 1, 1))
            year_end = min(TODAY - timedelta(days=1), date(year, 12, 31))
            if year_start > year_end:
                continue

            # 2-4 short sick episodes per year
            n_episodes = emp_rng.randint(2, 4)
            for _ in range(n_episodes):
                # Pick a random working day in the year as the start
                all_wdays = iter_working_days(year_start, year_end)
                if not all_wdays:
                    continue
                sick_start = emp_rng.choice(all_wdays)
                sick_len = emp_rng.randint(1, 3)
                sick_end = sick_start + timedelta(days=sick_len + emp_rng.randint(0, 1))
                for d in _working_days_in_range(sick_start, sick_end):
                    if d <= year_end:
                        absence_days[email].add(d)

        # --- Generate vacation blocks per year ---
        for year in range(emp_start.year, TODAY.year + 1):
            year_start = max(emp_start, date(year, 1, 1))
            year_end = min(TODAY - timedelta(days=1), date(year, 12, 31))
            if year_start > year_end:
                continue

            # Summer block: 2-3 weeks in July/August
            july1 = date(year, 7, 1)
            aug31 = date(year, 8, 31)
            if july1 >= year_start and aug31 <= year_end:
                summer_start_offset = emp_rng.randint(0, 40)
                summer_start = july1 + timedelta(days=summer_start_offset)
                summer_len = emp_rng.randint(10, 15)  # 2-3 weeks
                summer_end = summer_start + timedelta(days=summer_len + emp_rng.randint(0, 4))
                for d in _working_days_in_range(summer_start, min(summer_end, aug31)):
                    absence_days[email].add(d)

            # Christmas block: ~1 week around Dec 24-31
            dec20 = date(year, 12, 20)
            dec31 = date(year, 12, 31)
            if dec20 >= year_start and dec20 <= year_end:
                xmas_start = dec20 + timedelta(days=emp_rng.randint(0, 4))
                for d in _working_days_in_range(xmas_start, min(dec31, year_end)):
                    absence_days[email].add(d)

            # 1-2 misc short blocks (3-5 days each)
            n_misc = emp_rng.randint(1, 2)
            misc_months = emp_rng.sample([2, 3, 4, 5, 9, 10, 11], min(n_misc, 7))
            for m in misc_months:
                try:
                    block_start = date(year, m, emp_rng.randint(1, 20))
                except ValueError:
                    continue
                if block_start < year_start or block_start > year_end:
                    continue
                block_len = emp_rng.randint(3, 5)
                block_end = block_start + timedelta(days=block_len + 2)
                for d in _working_days_in_range(block_start, min(block_end, year_end)):
                    absence_days[email].add(d)

        # --- Special leaves ---
        for sl_email, sl_from, sl_to, _, _ in _SPECIAL_LEAVES:
            if sl_email == email:
                for d in _working_days_in_range(sl_from, sl_to):
                    absence_days[email].add(d)

    return absence_days


# ---------------------------------------------------------------------------
# Overtime / seasonal helpers
# ---------------------------------------------------------------------------


def _is_autumn_peak(d: date) -> bool:
    """September–November: autumn season peak."""
    return d.month in (9, 10, 11)


def _extra_overtime_minutes(email: str, d: date, rng: random.Random) -> int:
    """Return extra overtime minutes for a given employee on a given day."""
    base = 0
    # Seasonal autumn peak: +30 min most days
    if _is_autumn_peak(d) and rng.random() < 0.7:
        base += 30
    # Warehouse: 2-3 days/month extra hour (simulated as ~10% of days)
    dept = next(dept for _, _, em, dept, *_ in _USER_SPECS if em == email)
    if _is_warehouse(dept) and rng.random() < 0.12:
        base += 60
    # Overtime-prone employees
    if email in _OVERTIME_PRONE:
        base += rng.randint(10, 30)
    return base


# ---------------------------------------------------------------------------
# Clock history — 3 years
# ---------------------------------------------------------------------------


def _make_punch(
    tenant_id: uuid.UUID,
    employee_id: uuid.UUID,
    etype: EventType,
    etime: datetime,
    has_badge: bool,
    now: datetime,
) -> PunchEvent:
    src = EventSource.NFC_TERMINAL if has_badge else EventSource.WEB_PORTAL
    return PunchEvent(
        tenant_id=tenant_id,
        employee_id=employee_id,
        event_type=etype,
        event_time=etime,
        source=src,
        device_id="TERMINAL-01" if has_badge else None,
        created_at=now,
    )


def _make_timesheet(
    tenant_id: uuid.UUID,
    employee_id: uuid.UUID,
    work_date: date,
    punch_in: datetime,
    punch_out: datetime,
    break_start: datetime,
    break_end: datetime,
    contracted_daily_minutes: int,
    now: datetime,
) -> DailyTimesheet:
    gross = int((punch_out - punch_in).total_seconds() / 60)
    break_mins = int((break_end - break_start).total_seconds() / 60)
    net = max(0, gross - break_mins)
    overtime = max(0, net - contracted_daily_minutes)
    return DailyTimesheet(
        tenant_id=tenant_id,
        employee_id=employee_id,
        work_date=work_date,
        start_time=punch_in,
        end_time=punch_out,
        break_minutes=break_mins,
        net_work_minutes=net,
        gross_work_minutes=gross,
        overtime_minutes=overtime,
        status=TimesheetStatus.COMPLETE,
        is_night_work=False,
        is_sunday_work=False,
        is_holiday_work=False,
        computed_from_events=True,
        created_at=now,
        updated_at=now,
    )


async def generate_clock_history(
    session: AsyncSession,
    tenant: Tenant,
    users: dict[str, User],
    absence_days: dict[str, set[date]],
) -> dict[str, dict[date, int]]:
    """Generate 3 years of punch events and DailyTimesheet rows.

    Returns a mapping of email -> {date: net_work_minutes} for use in monthly summaries.
    Also returns overtime per day for Arbeitszeitkonto computation.
    """
    now = datetime.now(tz=UTC)

    # Build email -> dept, hours lookup
    user_meta: dict[str, tuple[str, int, date | None]] = {
        email: (dept, hours, hire) for _, _, email, dept, _, hours, _, hire in _USER_SPECS
    }

    # The "last 2 weeks" will be handled by recompute_daily_timesheet later
    last_2_weeks_start = working_days_back(TODAY, 10)[0]

    # Per-employee work log: {email: {date: (net_minutes, overtime_minutes)}}
    work_log: dict[str, dict[date, tuple[int, int]]] = {email: {} for email in users}

    all_days = iter_working_days(HISTORY_START, HISTORY_END)

    # Split into historical (batch) and recent (recompute)
    historical_days = [d for d in all_days if d < last_2_weeks_start]
    recent_days = [d for d in all_days if d >= last_2_weeks_start]

    print(f"  Historical days (direct insert): {len(historical_days)}")
    print(f"  Recent days (recompute): {len(recent_days)}")

    # ---- HISTORICAL: batch insert without recompute ----
    punch_batch: list[PunchEvent] = []
    timesheet_batch: list[DailyTimesheet] = []
    commit_interval = 100  # days

    # Track forgotten clockouts for the last 2 weeks demo scenario
    _FORGOT_CLOCKOUT: set[tuple[str, int]] = {
        ("m.mustermann@example.com", 3),
        ("k.fischer@example.com", 7),
        ("j.lange@example.com", 1),
    }

    days_processed = 0
    cur_month_label = ""

    for work_date in historical_days:
        month_label = f"{work_date.year}-{work_date.month:02d}"
        if month_label != cur_month_label:
            if cur_month_label:
                print(f"{cur_month_label} ", end="", flush=True)
            cur_month_label = month_label

        for email, user in users.items():
            dept, hours, hire_date = user_meta[email]
            emp_start = max(HISTORY_START, hire_date) if hire_date else HISTORY_START
            if work_date < emp_start:
                continue
            if work_date in absence_days[email]:
                continue

            contracted_daily = _CONTRACTED_DAILY_BY_HOURS.get(hours, 480)
            rng = random.Random(f"{email}-{work_date.isoformat()}")

            # Clock-in time
            if _is_warehouse(dept):
                base_min = 6 * 60 + 30
                jitter = rng.randint(-15, 15)
                jitter += rng.randint(0, 60)  # spread within window
            else:
                base_min = 7 * 60 + 30
                jitter = rng.randint(-15, 15)
                jitter += rng.randint(0, 60)
            ci_total = base_min + jitter
            punch_in = utc_dt(work_date, ci_total // 60, ci_total % 60)

            # Break around 12:00-12:30
            break_offset = rng.randint(270, 330)  # minutes after punch-in
            break_start = punch_in + timedelta(minutes=break_offset)
            break_len = rng.randint(28, 35)
            break_end = break_start + timedelta(minutes=break_len)

            # Base work time
            extra_ot = _extra_overtime_minutes(email, work_date, rng)
            base_work = contracted_daily + extra_ot + rng.randint(-10, 10)
            punch_out = punch_in + timedelta(minutes=base_work + break_len)
            # Ensure punch_out > break_end
            if punch_out <= break_end:
                punch_out = break_end + timedelta(minutes=rng.randint(90, 150))

            has_badge = user.nfc_badge_id is not None
            punch_batch.append(
                _make_punch(tenant.id, user.id, EventType.PUNCH_IN, punch_in, has_badge, now)
            )
            punch_batch.append(
                _make_punch(tenant.id, user.id, EventType.BREAK_START, break_start, has_badge, now)
            )
            punch_batch.append(
                _make_punch(tenant.id, user.id, EventType.BREAK_END, break_end, has_badge, now)
            )
            punch_batch.append(
                _make_punch(tenant.id, user.id, EventType.PUNCH_OUT, punch_out, has_badge, now)
            )

            ts = _make_timesheet(
                tenant.id,
                user.id,
                work_date,
                punch_in,
                punch_out,
                break_start,
                break_end,
                contracted_daily,
                now,
            )
            timesheet_batch.append(ts)
            work_log[email][work_date] = (ts.net_work_minutes, ts.overtime_minutes)

        days_processed += 1
        if days_processed % commit_interval == 0:
            session.add_all(punch_batch)
            session.add_all(timesheet_batch)
            await session.flush()
            punch_batch.clear()
            timesheet_batch.clear()

    # Flush remaining historical batches
    if punch_batch or timesheet_batch:
        session.add_all(punch_batch)
        session.add_all(timesheet_batch)
        await session.flush()
        punch_batch.clear()
        timesheet_batch.clear()

    print(f"{cur_month_label}")  # newline after last month
    print("  Historical punch events inserted.")

    # ---- RECENT: per-day with recompute ----
    print(f"  Generating recent {len(recent_days)} days with recompute...")
    for day_idx, work_date in enumerate(recent_days):
        for email, user in users.items():
            dept, hours, hire_date = user_meta[email]
            emp_start = max(HISTORY_START, hire_date) if hire_date else HISTORY_START
            if work_date < emp_start:
                continue
            if work_date in absence_days[email]:
                continue

            forgot_clockout = (email, day_idx) in _FORGOT_CLOCKOUT
            contracted_daily = _CONTRACTED_DAILY_BY_HOURS.get(hours, 480)
            rng = random.Random(f"{email}-{work_date.isoformat()}")

            if _is_warehouse(dept):
                base_min = 6 * 60 + 30
                jitter = rng.randint(-15, 15) + rng.randint(0, 60)
            else:
                base_min = 7 * 60 + 30
                jitter = rng.randint(-15, 15) + rng.randint(0, 60)
            ci_total = base_min + jitter
            punch_in = utc_dt(work_date, ci_total // 60, ci_total % 60)

            break_offset = rng.randint(270, 330)
            break_start_t = punch_in + timedelta(minutes=break_offset)
            break_len = rng.randint(28, 35)
            break_end_t = break_start_t + timedelta(minutes=break_len)

            has_badge = user.nfc_badge_id is not None
            session.add(
                _make_punch(tenant.id, user.id, EventType.PUNCH_IN, punch_in, has_badge, now)
            )
            session.add(
                _make_punch(
                    tenant.id, user.id, EventType.BREAK_START, break_start_t, has_badge, now
                )
            )
            session.add(
                _make_punch(tenant.id, user.id, EventType.BREAK_END, break_end_t, has_badge, now)
            )

            if not forgot_clockout:
                extra_ot = _extra_overtime_minutes(email, work_date, rng)
                base_work = contracted_daily + extra_ot + rng.randint(-10, 10)
                punch_out = punch_in + timedelta(minutes=base_work + break_len)
                if punch_out <= break_end_t:
                    punch_out = break_end_t + timedelta(minutes=rng.randint(90, 150))
                session.add(
                    _make_punch(tenant.id, user.id, EventType.PUNCH_OUT, punch_out, has_badge, now)
                )

        await session.flush()
        for email, user in users.items():
            dept, hours, hire_date = user_meta[email]
            emp_start = max(HISTORY_START, hire_date) if hire_date else HISTORY_START
            if work_date < emp_start:
                continue
            if work_date in absence_days[email]:
                continue
            ts = await recompute_daily_timesheet(session, tenant.id, user.id, work_date)
            work_log[email][work_date] = (ts.net_work_minutes, ts.overtime_minutes)

    print("  Clock history done.")
    return work_log


# ---------------------------------------------------------------------------
# Absence requests (historical + demo scenarios)
# ---------------------------------------------------------------------------


async def _add_approval_request(
    session: AsyncSession,
    tenant: Tenant,
    employee: User,
    admin: User,
    req_type: RequestType,
    status: RequestStatus,
    date_from: date,
    date_to: date,
    notes: str,
    submitted_at: datetime,
    payload: dict | None = None,
    decided_at: datetime | None = None,
    decision_reason: str | None = None,
) -> ApprovalRequest:
    days = _count_working_days(date_from, date_to)
    req = ApprovalRequest(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        employee_id=employee.id,
        request_type=req_type,
        status=status,
        date_from=date_from,
        date_to=date_to,
        days_count=Decimal(str(days)),
        payload=payload,
        notes=notes,
        submitted_at=submitted_at,
        decided_by=admin.id if status in (RequestStatus.APPROVED, RequestStatus.REJECTED) else None,
        decided_at=decided_at,
        decision_reason=decision_reason,
    )
    session.add(req)
    await session.flush()

    session.add(
        ApprovalAuditLog(
            request_id=req.id,
            old_status=None,
            new_status=RequestStatus.SUBMITTED,
            actor_id=employee.id,
            actor_type=ActorType.USER,
            created_at=submitted_at,
        )
    )
    if status == RequestStatus.APPROVED and decided_at:
        session.add(
            ApprovalAuditLog(
                request_id=req.id,
                old_status=RequestStatus.SUBMITTED,
                new_status=RequestStatus.APPROVED,
                actor_id=admin.id,
                actor_type=ActorType.USER,
                notes=decision_reason or "Genehmigt",
                created_at=decided_at,
            )
        )
    elif status == RequestStatus.REJECTED and decided_at:
        session.add(
            ApprovalAuditLog(
                request_id=req.id,
                old_status=RequestStatus.SUBMITTED,
                new_status=RequestStatus.REJECTED,
                actor_id=admin.id,
                actor_type=ActorType.USER,
                notes=decision_reason or "Abgelehnt",
                created_at=decided_at,
            )
        )
    return req


async def generate_historical_absences(
    session: AsyncSession,
    tenant: Tenant,
    users: dict[str, User],
    absence_days: dict[str, set[date]],
) -> None:
    """Create ApprovalRequest records for all historical sick/vacation/special absences."""
    admin = users["l.hoffmann@example.com"]
    datetime.now(tz=UTC)
    count = 0

    user_meta: dict[str, tuple[str, int, date | None]] = {
        email: (dept, hours, hire) for _, _, email, dept, _, hours, _, hire in _USER_SPECS
    }

    print("  Generating historical absence requests...")

    for email, user in users.items():
        dept, hours, hire_date = user_meta[email]
        emp_start = max(HISTORY_START, hire_date) if hire_date else HISTORY_START
        emp_rng = random.Random(f"absence-req-{email}")

        # --- Langzeitkrankheit ---
        if email == _LANGZEIT_EMPLOYEE:
            submitted = utc_dt(_LANGZEIT_START, 7, 30)
            decided = utc_dt(_LANGZEIT_START, 9, 0)
            await _add_approval_request(
                session,
                tenant,
                user,
                admin,
                RequestType.SICK_LEAVE,
                RequestStatus.APPROVED,
                _LANGZEIT_START,
                _LANGZEIT_END,
                "Langzeitkrankheit (Attest liegt vor)",
                submitted,
                decided_at=decided,
                decision_reason="Automatisch genehmigt (Langzeitkrankheit)",
            )
            count += 1

        # --- Reconstruct vacation blocks from absence calendar ---
        # Group consecutive absence days into blocks per year
        all_abs_dates = sorted(
            d
            for d in absence_days[email]
            if emp_start <= d < TODAY and d < (TODAY + timedelta(days=-23))
        )  # historical only

        # Remove sick days we know about (langzeit) to separate vacation
        langzeit_dates: set[date] = set()
        if email == _LANGZEIT_EMPLOYEE:
            langzeit_dates = set(_working_days_in_range(_LANGZEIT_START, _LANGZEIT_END))

        # Build consecutive blocks
        blocks: list[tuple[date, date]] = []
        if all_abs_dates:
            block_start = all_abs_dates[0]
            block_prev = all_abs_dates[0]
            for d in all_abs_dates[1:]:
                gap = (d - block_prev).days
                if gap <= 5:  # allow weekends within block
                    block_prev = d
                else:
                    blocks.append((block_start, block_prev))
                    block_start = d
                    block_prev = d
            blocks.append((block_start, block_prev))

        for b_from, b_to in blocks:
            if b_from in langzeit_dates or b_to in langzeit_dates:
                continue
            # Determine if sick or vacation based on heuristics:
            # Blocks <= 3 days → sick; longer → vacation
            wdays = _count_working_days(b_from, b_to)
            if wdays == 0:
                continue
            if wdays <= 3 and emp_rng.random() < 0.4:
                notes_text = emp_rng.choice(["Erkältung", "Grippe", "Magen-Darm", "Kopfschmerzen"])
                sub_h = emp_rng.randint(7, 9)
                submitted = utc_dt(b_from, sub_h, emp_rng.randint(0, 59))
                decided = submitted + timedelta(hours=1)
                await _add_approval_request(
                    session,
                    tenant,
                    user,
                    admin,
                    RequestType.SICK_LEAVE,
                    RequestStatus.APPROVED,
                    b_from,
                    b_to,
                    notes_text,
                    submitted,
                    decided_at=decided,
                    decision_reason="Automatisch genehmigt (Krankmeldung)",
                )
            else:
                notes_text = emp_rng.choice(["Urlaub", "Familienurlaub", "Reise", "Erholung"])
                sub_days_before = emp_rng.randint(7, 21)
                sub_date = b_from - timedelta(days=sub_days_before)
                submitted = utc_dt(sub_date, emp_rng.randint(8, 17), emp_rng.randint(0, 59))
                decided = submitted + timedelta(days=emp_rng.randint(1, 3))
                await _add_approval_request(
                    session,
                    tenant,
                    user,
                    admin,
                    RequestType.VACATION,
                    RequestStatus.APPROVED,
                    b_from,
                    b_to,
                    notes_text,
                    submitted,
                    decided_at=decided,
                    decision_reason="Genehmigt",
                )
            count += 1

        # --- Special leaves ---
        for sl_email, sl_from, sl_to, reason_code, reason_text in _SPECIAL_LEAVES:
            if sl_email != email:
                continue
            if sl_from < emp_start:
                continue
            sub_days_before = emp_rng.randint(5, 14)
            sub_date = sl_from - timedelta(days=sub_days_before)
            submitted = utc_dt(sub_date, emp_rng.randint(8, 17), emp_rng.randint(0, 59))
            decided = submitted + timedelta(days=emp_rng.randint(1, 5))
            await _add_approval_request(
                session,
                tenant,
                user,
                admin,
                RequestType.SPECIAL_LEAVE,
                RequestStatus.APPROVED,
                sl_from,
                sl_to,
                f"Sonderurlaub: {reason_text}",
                submitted,
                payload={"reason_code": reason_code, "description": reason_text},
                decided_at=decided,
                decision_reason="Genehmigt gemäß Betriebsvereinbarung §7 Abs. 2",
            )
            count += 1

    await session.flush()
    print(f"  Historical absence requests: {count} created")


# ---------------------------------------------------------------------------
# Demo scenarios for March 2026
# ---------------------------------------------------------------------------


async def generate_demo_absences(
    session: AsyncSession,
    tenant: Tenant,
    users: dict[str, User],
) -> None:
    """Create the 6 demo absence request scenarios for current/near-future dates."""
    datetime.now(tz=UTC)
    admin = users["l.hoffmann@example.com"]

    # --- 1. Urlaub SUBMITTED (overlap test): Max Mustermann Mar 30 – Apr 3 ---
    mustermann = users["m.mustermann@example.com"]
    req1 = ApprovalRequest(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        employee_id=mustermann.id,
        request_type=RequestType.VACATION,
        status=RequestStatus.SUBMITTED,
        date_from=(TODAY + timedelta(days=6)),
        date_to=(TODAY + timedelta(days=10)),
        days_count=Decimal("5"),
        notes="Osterurlaub",
        submitted_at=utc_dt((TODAY + timedelta(days=-5)), 9, 14),
    )
    session.add(req1)
    await session.flush()
    session.add(
        ApprovalAuditLog(
            request_id=req1.id,
            old_status=None,
            new_status=RequestStatus.SUBMITTED,
            actor_id=mustermann.id,
            actor_type=ActorType.USER,
            notes="Urlaubsantrag eingereicht",
            created_at=utc_dt((TODAY + timedelta(days=-5)), 9, 14),
        )
    )

    # --- 2. Urlaub APPROVED (overlap test): Erika Schmidt Mar 31 – Apr 2 ---
    schmidt = users["e.schmidt@example.com"]
    req2 = ApprovalRequest(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        employee_id=schmidt.id,
        request_type=RequestType.VACATION,
        status=RequestStatus.APPROVED,
        date_from=(TODAY + timedelta(days=7)),
        date_to=(TODAY + timedelta(days=9)),
        days_count=Decimal("3"),
        notes="Kurzurlaub",
        submitted_at=utc_dt((TODAY + timedelta(days=-10)), 11, 0),
        decided_by=admin.id,
        decided_at=utc_dt((TODAY + timedelta(days=-9)), 8, 30),
        decision_reason="Genehmigt",
    )
    session.add(req2)
    await session.flush()
    session.add(
        ApprovalAuditLog(
            request_id=req2.id,
            old_status=None,
            new_status=RequestStatus.SUBMITTED,
            actor_id=schmidt.id,
            actor_type=ActorType.USER,
            created_at=utc_dt((TODAY + timedelta(days=-10)), 11, 0),
        )
    )
    session.add(
        ApprovalAuditLog(
            request_id=req2.id,
            old_status=RequestStatus.SUBMITTED,
            new_status=RequestStatus.APPROVED,
            actor_id=admin.id,
            actor_type=ActorType.USER,
            notes="Genehmigt",
            created_at=utc_dt((TODAY + timedelta(days=-9)), 8, 30),
        )
    )

    # --- 3. Urlaub SUBMITTED (understaffing): Klaus Fischer Mar 30 – Apr 1 ---
    fischer = users["k.fischer@example.com"]
    req3 = ApprovalRequest(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        employee_id=fischer.id,
        request_type=RequestType.VACATION,
        status=RequestStatus.SUBMITTED,
        date_from=(TODAY + timedelta(days=6)),
        date_to=(TODAY + timedelta(days=8)),
        days_count=Decimal("3"),
        notes="Brücke vor Ostern",
        submitted_at=utc_dt((TODAY + timedelta(days=-4)), 14, 22),
    )
    session.add(req3)
    await session.flush()
    session.add(
        ApprovalAuditLog(
            request_id=req3.id,
            old_status=None,
            new_status=RequestStatus.SUBMITTED,
            actor_id=fischer.id,
            actor_type=ActorType.USER,
            created_at=utc_dt((TODAY + timedelta(days=-4)), 14, 22),
        )
    )

    # --- 4. Approved vacation: Anna Wagner Apr 7–11 ---
    wagner = users["a.wagner@example.com"]
    req4 = ApprovalRequest(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        employee_id=wagner.id,
        request_type=RequestType.VACATION,
        status=RequestStatus.APPROVED,
        date_from=(TODAY + timedelta(days=14)),
        date_to=(TODAY + timedelta(days=18)),
        days_count=Decimal("5"),
        notes="Familienurlaub",
        submitted_at=utc_dt((TODAY + timedelta(days=-19)), 10, 0),
        decided_by=admin.id,
        decided_at=utc_dt((TODAY + timedelta(days=-18)), 9, 0),
        decision_reason="Genehmigt",
    )
    session.add(req4)
    await session.flush()
    session.add(
        ApprovalAuditLog(
            request_id=req4.id,
            old_status=None,
            new_status=RequestStatus.SUBMITTED,
            actor_id=wagner.id,
            actor_type=ActorType.USER,
            created_at=utc_dt((TODAY + timedelta(days=-19)), 10, 0),
        )
    )
    session.add(
        ApprovalAuditLog(
            request_id=req4.id,
            old_status=RequestStatus.SUBMITTED,
            new_status=RequestStatus.APPROVED,
            actor_id=admin.id,
            actor_type=ActorType.USER,
            notes="Genehmigt",
            created_at=utc_dt((TODAY + timedelta(days=-18)), 9, 0),
        )
    )

    # --- 5. Rejected request: Peter Becker Mar 24–26 ---
    becker = users["p.becker@example.com"]
    req5 = ApprovalRequest(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        employee_id=becker.id,
        request_type=RequestType.VACATION,
        status=RequestStatus.REJECTED,
        date_from=(TODAY + timedelta(days=0)),
        date_to=(TODAY + timedelta(days=2)),
        days_count=Decimal("3"),
        notes="Kurzfristiger Urlaub",
        submitted_at=utc_dt((TODAY + timedelta(days=-6)), 15, 30),
        decided_by=admin.id,
        decided_at=utc_dt((TODAY + timedelta(days=-5)), 10, 0),
        decision_reason="Zu viele Abwesenheiten im Team",
    )
    session.add(req5)
    await session.flush()
    session.add(
        ApprovalAuditLog(
            request_id=req5.id,
            old_status=None,
            new_status=RequestStatus.SUBMITTED,
            actor_id=becker.id,
            actor_type=ActorType.USER,
            created_at=utc_dt((TODAY + timedelta(days=-6)), 15, 30),
        )
    )
    session.add(
        ApprovalAuditLog(
            request_id=req5.id,
            old_status=RequestStatus.SUBMITTED,
            new_status=RequestStatus.REJECTED,
            actor_id=admin.id,
            actor_type=ActorType.USER,
            notes="Zu viele Abwesenheiten im Team",
            created_at=utc_dt((TODAY + timedelta(days=-5)), 10, 0),
        )
    )

    # --- 6. Special leave: Sabine Koch Mar 28 (Umzug) ---
    koch = users["s.koch@example.com"]
    req6 = ApprovalRequest(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        employee_id=koch.id,
        request_type=RequestType.SPECIAL_LEAVE,
        status=RequestStatus.APPROVED,
        date_from=(TODAY + timedelta(days=4)),
        date_to=(TODAY + timedelta(days=4)),
        days_count=Decimal("1"),
        payload={"reason_code": "UMZUG", "description": "Wohnungsumzug"},
        notes="Sonderurlaub für Umzug",
        submitted_at=utc_dt((TODAY + timedelta(days=-14)), 12, 0),
        decided_by=admin.id,
        decided_at=utc_dt((TODAY + timedelta(days=-13)), 9, 0),
        decision_reason="Genehmigt gemäß Betriebsvereinbarung §7 Abs. 2",
    )
    session.add(req6)
    await session.flush()
    session.add(
        ApprovalAuditLog(
            request_id=req6.id,
            old_status=None,
            new_status=RequestStatus.SUBMITTED,
            actor_id=koch.id,
            actor_type=ActorType.USER,
            created_at=utc_dt((TODAY + timedelta(days=-14)), 12, 0),
        )
    )
    session.add(
        ApprovalAuditLog(
            request_id=req6.id,
            old_status=RequestStatus.SUBMITTED,
            new_status=RequestStatus.APPROVED,
            actor_id=admin.id,
            actor_type=ActorType.USER,
            notes="Genehmigt gemäß Betriebsvereinbarung §7 Abs. 2",
            created_at=utc_dt((TODAY + timedelta(days=-13)), 9, 0),
        )
    )

    await session.flush()
    print("  Demo absence requests: 6 scenarios created")


# ---------------------------------------------------------------------------
# Monthly summaries — full 3-year range
# ---------------------------------------------------------------------------


async def generate_monthly_summaries(
    session: AsyncSession,
    tenant: Tenant,
    users: dict[str, User],
    work_log: dict[str, dict[date, tuple[int, int]]],
    absence_days: dict[str, set[date]],
) -> None:
    """Generate MonthlySummary rows for every employee for every month.

    Uses work_log (from clock history) for accurate totals. Absence days are
    classified as sick or vacation based on heuristic matching with the
    absence calendar.
    """
    now = datetime.now(tz=UTC)

    user_meta: dict[str, tuple[str, int, date | None]] = {
        email: (dept, hours, hire) for _, _, email, dept, _, hours, _, hire in _USER_SPECS
    }

    # Months to generate: March 2023 → February 2026 (last complete month before today)
    summary_start = HISTORY_START
    summary_end_month = TODAY + timedelta(days=-51)
    months = iter_months(summary_start, summary_end_month)

    summaries: list[MonthlySummary] = []

    for email, user in users.items():
        dept, hours, hire_date = user_meta[email]
        emp_start = max(HISTORY_START, hire_date) if hire_date else HISTORY_START
        contracted_daily = _CONTRACTED_DAILY_BY_HOURS.get(hours, 480)
        contracted_daily * 20  # ~20 working days per month

        arbeitszeitkonto_balance = 0  # running cumulative

        for ym in months:
            if ym < date(emp_start.year, emp_start.month, 1):
                continue

            # Find all working days in this month
            month_end = (
                date(ym.year, ym.month + 1, 1) - timedelta(days=1)
                if ym.month < 12
                else date(ym.year, 12, 31)
            )
            wdays_in_month = iter_working_days(ym, month_end)

            # Public holidays in this month
            hh_holidays = get_holidays(ym.year, "HH")
            days_holiday = sum(
                1
                for d in wdays_in_month
                # Actually wdays already exclude holidays, so count separately
                # Let's count calendar days that are holidays (not weekend)
                if False  # wdays excludes holidays, need separate count
            )
            # Re-count: all Mon-Fri that ARE holidays
            all_weekdays_in_month: list[date] = []
            cur = ym
            while cur <= month_end:
                if not is_weekend(cur):
                    all_weekdays_in_month.append(cur)
                cur += timedelta(days=1)
            days_holiday = sum(1 for d in all_weekdays_in_month if d in hh_holidays)

            # Days absent (from absence calendar)
            days_absent_in_month = {d for d in absence_days[email] if ym <= d <= month_end}

            # Heuristic: split absent days into sick vs vacation
            # Use a deterministic rng to match the pattern used in absence calendar
            emp_rng = random.Random(f"summary-split-{email}-{ym.isoformat()}")

            # Langzeitkrankheit months: Wolfgang Schulz
            langzeit_days_in_month: set[date] = set()
            if email == _LANGZEIT_EMPLOYEE:
                langzeit_days_in_month = {
                    d
                    for d in _working_days_in_range(_LANGZEIT_START, _LANGZEIT_END)
                    if ym <= d <= month_end
                }

            days_sick = len(langzeit_days_in_month)
            days_vacation = 0
            other_absent = days_absent_in_month - langzeit_days_in_month
            # Short blocks (<=3 consecutive) are more likely sick; longer are vacation
            # We simplify: group consecutive days and classify
            sorted_absent = sorted(other_absent)
            if sorted_absent:
                run_start = sorted_absent[0]
                run_end = sorted_absent[0]
                for d in sorted_absent[1:]:
                    if (d - run_end).days <= 3:
                        run_end = d
                    else:
                        run_len = len([x for x in sorted_absent if run_start <= x <= run_end])
                        if run_len <= 2 and emp_rng.random() < 0.4:
                            days_sick += run_len
                        else:
                            days_vacation += run_len
                        run_start = d
                        run_end = d
                # Last run
                run_len = len([x for x in sorted_absent if run_start <= x <= run_end])
                if run_len <= 2 and emp_rng.random() < 0.4:
                    days_sick += run_len
                else:
                    days_vacation += run_len

            # Work log data for this month
            month_work = {d: v for d, v in work_log.get(email, {}).items() if ym <= d <= month_end}
            total_work_minutes = sum(net for net, _ in month_work.values())
            total_overtime_minutes = sum(max(0, ot) for _, ot in month_work.values())
            days_worked = len(month_work)

            # Update running Arbeitszeitkonto
            arbeitszeitkonto_balance += total_overtime_minutes

            # Ausgleich: employees with high OT take comp time occasionally
            if email in _OVERTIME_PRONE and arbeitszeitkonto_balance > 20 * 60:
                # 2-3 Ausgleich days per quarter (roughly)
                if emp_rng.random() < 0.3:
                    ausgleich_days = emp_rng.randint(1, 2)
                    ausgleich_minutes = ausgleich_days * contracted_daily
                    arbeitszeitkonto_balance = max(0, arbeitszeitkonto_balance - ausgleich_minutes)
                    days_vacation += ausgleich_days

            summaries.append(
                MonthlySummary(
                    tenant_id=tenant.id,
                    employee_id=user.id,
                    year_month=ym,
                    total_work_minutes=total_work_minutes,
                    total_overtime_minutes=total_overtime_minutes,
                    days_worked=days_worked,
                    days_sick=days_sick,
                    days_vacation=days_vacation,
                    days_holiday=days_holiday,
                    night_supplement_minutes=0,
                    sunday_supplement_minutes=0,
                    holiday_supplement_minutes=0,
                    arbeitszeitkonto_balance_minutes=arbeitszeitkonto_balance,
                    created_at=now,
                    updated_at=now,
                )
            )

    session.add_all(summaries)
    await session.flush()
    print(f"  Monthly summaries: {len(summaries)} rows inserted")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Today's presence — employees currently at work
# ---------------------------------------------------------------------------

# Demo accounts that should NOT get today punches (they are for login only)
_DEMO_EMAILS = {
    "gf@example.com",
    "hr@example.com",
    "teamleiter@example.com",
    "teamleiter2@example.com",
    "mitarbeiter@example.com",
    "mitarbeiter2@example.com",
    "buchhaltung@example.com",
    "betriebsrat@example.com",
}


async def seed_today_presence(
    session: AsyncSession,
    tenant: Tenant,
    users: dict[str, User],
) -> None:
    """Seed 'today' punch events so ~65% of real employees are currently clocked in.

    This gives the live presence feature something to display in demos.
    """
    now = datetime.now(tz=UTC)
    rng = random.Random("today-presence")

    # Filter to real employees only (exclude demo accounts)
    real_employees = [(email, user) for email, user in users.items() if email not in _DEMO_EMAILS]

    # Pick ~65% to be "present today"
    n_present = int(len(real_employees) * 0.65)
    rng.shuffle(real_employees)
    present = real_employees[:n_present]
    # remaining = real_employees[n_present:]  # absent today

    user_meta: dict[str, tuple[str, int, date | None]] = {
        email: (dept, hours, hire) for _, _, email, dept, _, hours, _, hire in _USER_SPECS
    }

    clocked_in_count = 0
    on_break_count = 0

    for email, user in present:
        dept, hours, hire_date = user_meta[email]
        has_badge = user.nfc_badge_id is not None

        # Realistic clock-in time
        if _is_warehouse(dept):
            hour = rng.randint(6, 7)
            minute = rng.randint(0, 45)
        else:
            hour = rng.randint(7, 8)
            minute = rng.randint(0, 45)

        punch_in_time = utc_dt(TODAY, hour, minute)
        session.add(
            _make_punch(tenant.id, user.id, EventType.PUNCH_IN, punch_in_time, has_badge, now)
        )

        # ~30% have taken and completed a break
        # ~10% are currently on break
        roll = rng.random()
        if roll < 0.30:
            # Completed break
            bs = punch_in_time + timedelta(minutes=rng.randint(240, 300))
            be = bs + timedelta(minutes=rng.randint(28, 35))
            session.add(_make_punch(tenant.id, user.id, EventType.BREAK_START, bs, has_badge, now))
            session.add(_make_punch(tenant.id, user.id, EventType.BREAK_END, be, has_badge, now))
            clocked_in_count += 1
        elif roll < 0.40:
            # Currently on break
            bs = punch_in_time + timedelta(minutes=rng.randint(240, 300))
            session.add(_make_punch(tenant.id, user.id, EventType.BREAK_START, bs, has_badge, now))
            on_break_count += 1
        else:
            # Just clocked in, working
            clocked_in_count += 1

    await session.flush()

    # Recompute timesheets for today
    for _email, user in present:
        await recompute_daily_timesheet(session, tenant.id, user.id, TODAY)

    print(
        f"  Today presence: {len(present)} present ({clocked_in_count} working, {on_break_count} on break), "
        f"{len(real_employees) - len(present)} absent"
    )


async def main() -> None:
    settings = get_settings()
    print(f"Connecting to: {settings.database_url}")

    engine = create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        echo=False,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async with session_factory() as session:
        print("\n[1/8] Clearing existing data...")
        await clear_all(session)

        print("\n[2/8] Creating tenant...")
        tenant = await create_tenant(session)
        await session.commit()

        print("\n[3/8] Creating roles...")
        roles = await create_roles(session, tenant)
        await session.commit()

        print("\n[3b/8] Creating departments...")
        departments = await create_departments(session, tenant)
        await session.commit()

        print("\n[4/8] Creating users...")
        users = await create_users(session, tenant, roles, departments)
        await session.commit()

        print("\n[5/8] Building absence calendar...")
        absence_days = _build_absence_calendar(users)
        total_abs = sum(len(v) for v in absence_days.values())
        print(f"  Absence calendar: {total_abs} employee-days marked absent")

        print("\n[6/8] Generating 3-year clock history (March 2023 – March 2026)...")
        print("  Progress: ", end="", flush=True)
        work_log = await generate_clock_history(session, tenant, users, absence_days)
        total_punches = sum(len(v) for v in work_log.values())
        print(f"  Work log: {total_punches} employee-days with punch data")
        await session.commit()

        print("\n[7/8] Generating absence requests...")
        await generate_historical_absences(session, tenant, users, absence_days)
        await generate_demo_absences(session, tenant, users)
        await session.commit()

        print("\n[8/9] Generating monthly summaries (March 2023 – February 2026)...")
        await generate_monthly_summaries(session, tenant, users, work_log, absence_days)
        await session.commit()

        print("\n[9/9] Seeding today's live presence...")
        await seed_today_presence(session, tenant, users)
        await session.commit()

    await engine.dispose()

    print("\nDemo seed complete.")
    print("  Tenant:     Muster GmbH")
    print(
        f"  Users:      {len(_USER_SPECS)} ({len(_USER_SPECS) - 6} real employees + 6 demo accounts)"
    )
    print("  Logins:     admins -> admin123  |  others -> demo123")
    print(f"  Date range: {HISTORY_START} – {TODAY} (3 years)")
    print(f"  Demo date:  {TODAY}")


if __name__ == "__main__":
    asyncio.run(main())
