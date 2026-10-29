"""Shared pytest fixtures.

Uses an in-memory SQLite database via aiosqlite for fast unit tests.
All tests that need a real DB connection can override the engine fixture.
"""

from __future__ import annotations

import os

# Must run before any `zeiterfassung.*` import triggers `get_settings()`
# (several modules call it at import time, e.g. core/security.py). Without a
# real secret here, `create_app()`'s startup check (fkw.4, main.py) would
# raise RuntimeError for every test — settings.debug is False by default and
# the untouched default is the CHANGE_ME placeholder. This mirrors a real
# deployment (a proper secret configured), not `debug=True`.
os.environ.setdefault(
    "ZEITERFASSUNG_JWT_SECRET",
    "test-only-secret-do-not-use-in-prod-0123456789abcdef",
)

from collections.abc import AsyncGenerator  # noqa: E402
from datetime import UTC, datetime  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy import DateTime as SADateTime  # noqa: E402
from sqlalchemy.dialects.sqlite.base import DATETIME as SQLiteDATETIME  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# Import all models so their tables are registered on Base.metadata before create_all
import zeiterfassung.models  # noqa: E402, F401
from zeiterfassung.models.base import Base  # noqa: E402
from zeiterfassung.models.tenant import Tenant  # noqa: E402
from zeiterfassung.models.user import User  # noqa: E402

# ---------------------------------------------------------------------------
# Async engine (SQLite in-memory — no postgres required for unit tests)
# ---------------------------------------------------------------------------

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


class TZAwareSQLiteDateTime(SQLiteDATETIME):
    """SQLite ``DATETIME`` that mirrors Postgres ``TIMESTAMP WITH TIME ZONE``.

    SQLAlchemy's stock SQLite ``DATETIME`` stores the *wall-clock* digits of a
    tz-aware ``datetime`` and silently drops the UTC offset (values round-trip
    as naive) — unlike Postgres, which normalizes ``timestamptz`` columns to
    UTC internally and always returns tz-aware datetimes to asyncpg. That gap
    let timezone bugs slip through the in-memory SQLite test suite while
    still breaking on real Postgres (zeiterfassung-l1bx).

    This type closes the gap for tests only: on bind, a tz-aware value is
    converted to UTC before being handed to the stock storage format; on
    read, a naive result is re-attached with ``tzinfo=UTC``. Naive-by-design
    columns (``DateTime(timezone=False)``, none currently exist in this
    codebase) are left untouched via the ``self.timezone`` guard, and
    Postgres is never touched — this class is only ever registered on the
    SQLite test engine's dialect instance (see the ``engine`` fixture below).
    """

    def bind_processor(self, dialect):
        parent_process = super().bind_processor(dialect)

        def process(value):
            if isinstance(value, datetime) and self.timezone and value.tzinfo is not None:
                value = value.astimezone(UTC).replace(tzinfo=None)
            return parent_process(value)

        return process

    def result_processor(self, dialect, coltype):
        parent_process = super().result_processor(dialect, coltype)

        def process(value):
            result = parent_process(value)
            if isinstance(result, datetime) and self.timezone and result.tzinfo is None:
                result = result.replace(tzinfo=UTC)
            return result

        return process


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest_asyncio.fixture(scope="function")
async def engine():
    """Create a fresh SQLite in-memory engine for each test function.

    SQLite quirks handled:
    - BigInteger PKs: SQLite treats any INTEGER PK as autoincrement.
      We override BigInteger to Integer in table creation for SQLite.
    - ENUM columns: SQLite doesn't have native ENUM, so we use
      native_enum=False (SQLAlchemy renders as VARCHAR).
    - tz-aware DateTime columns: SQLite drops the UTC offset by default
      (see `TZAwareSQLiteDateTime` above) — we register a UTC-normalizing
      override on this engine's dialect *instance* only, so production
      Postgres behavior and other SQLite engines/tests are unaffected.
    """
    from sqlalchemy import event as sa_event

    eng = create_async_engine(TEST_DATABASE_URL, echo=False)

    # Instance-level override (not a mutation of the shared dialect-class
    # dict) so this only applies to this test engine.
    eng.sync_engine.dialect.colspecs = {
        **eng.sync_engine.dialect.colspecs,
        SADateTime: TZAwareSQLiteDateTime,
    }

    # Patch BigInteger -> Integer for SQLite so autoincrement PKs work
    @sa_event.listens_for(eng.sync_engine, "before_cursor_execute")
    def receive_before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        pass  # no-op, just ensuring event listener is registered

    async with eng.begin() as conn:
        # Create tables using metadata but with SQLite-safe enum handling
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                sync_conn,
                checkfirst=True,
            )
        )
    yield eng
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await eng.dispose()


@pytest_asyncio.fixture(scope="function")
async def db(engine) -> AsyncGenerator[AsyncSession, None]:
    """Yield a DB session bound to the test engine."""
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as session:
        yield session


# ---------------------------------------------------------------------------
# Domain fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def tenant(db: AsyncSession) -> Tenant:
    now = datetime.now(tz=UTC)
    t = Tenant(
        name="Test GmbH",
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
async def employee(db: AsyncSession, tenant: Tenant) -> User:
    from zeiterfassung.core.security import hash_password

    now = datetime.now(tz=UTC)
    u = User(
        tenant_id=tenant.id,
        email="max.mustermann@test.de",
        hashed_password=hash_password("Passw0rd!"),
        first_name="Max",
        last_name="Mustermann",
        employee_number="EMP001",
        nfc_badge_id="BADGE001",
        contracted_hours_per_week=40.0,
        # Tests rechnen mit 24 Tagen; /absences/balance nutzt jetzt das
        # Feld vacation_entitlement_days (Default wäre 30).
        vacation_entitlement_days=24.0,
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    db.add(u)
    await db.flush()
    return u


async def ensure_login_account(
    db: AsyncSession,
    tenant_id,
    account_id=None,
    *,
    employee_id=None,
    email: str | None = None,
):
    """Account-Zeile fuer selbstgebaute JWTs anlegen.

    get_current_user macht seit login-hardening einen DB-Lookup auf ``sub`` —
    ein Token fuer eine Account-ID ohne DB-Zeile bekommt 401. Tests, die
    Tokens mit ``create_access_token(user_id=...)`` minten, rufen vorher
    diesen Helper mit derselben ID auf.
    """
    import uuid as _uuid

    from zeiterfassung.core.security import hash_password
    from zeiterfassung.models.account import Account

    now = datetime.now(tz=UTC)
    acc = Account(
        id=account_id or _uuid.uuid4(),
        tenant_id=tenant_id,
        email=email or f"acct-{_uuid.uuid4().hex[:10]}@test.de",
        hashed_password=hash_password("Passw0rd!"),
        is_active=True,
        employee_id=employee_id,
        created_at=now,
        updated_at=now,
    )
    db.add(acc)
    await db.flush()
    return acc


@pytest.fixture(autouse=True)
def _reset_auth_rate_limits():
    from zeiterfassung.core import rate_limit

    rate_limit.reset()
    yield
    rate_limit.reset()
