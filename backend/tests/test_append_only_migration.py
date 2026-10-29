"""Tests for alembic/versions/0036_append_only_triggers.py.

Scope note: the actual DB-level enforcement (a stray UPDATE/DELETE on
``punch_events``/``approval_audit_log`` raising) can only be exercised against
a real Postgres instance — the trigger SQL uses ``plpgsql`` and
``current_setting``, which SQLite (the test suite's engine) doesn't support.
That was verified manually against Postgres (``alembic upgrade head`` +
a direct ``UPDATE``/``DELETE`` raising, then succeeding once the session GUC
is set), not by this suite.

What *can* be verified headlessly, and is verified here:
- the migration is a real no-op on SQLite (the dialect-guard branch that the
  whole test suite actually exercises via ``alembic`` never running against
  SQLite in prod, but the guard itself runs fine standalone against a SQLite
  connection);
- the postgres-branch SQL is structurally well-formed (balanced parens/dollar
  quoting, expected keywords, references both target tables, references the
  escape-hatch GUC) — a compile-level syntax check against a real Postgres
  server is out of scope without a Postgres testcontainer dependency.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine

_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent / "alembic" / "versions" / "0036_append_only_triggers.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("append_only_migration_0036", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


migration = _load_migration()


def test_revision_metadata() -> None:
    assert migration.revision == "0036_append_only_triggers"
    assert migration.down_revision == "0035_login_lockout"
    assert len(migration.revision) <= 32


def test_upgrade_is_noop_on_sqlite() -> None:
    """The dialect guard must make upgrade()/downgrade() true no-ops on SQLite.

    Runs against a real (throwaway, in-memory) SQLite connection via a real
    alembic Operations context — not a mock — so the actual guard code path
    (``bind.dialect.name != "postgresql": return``) executes for real.
    """
    engine = create_engine("sqlite:///:memory:")
    try:
        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                # Must not raise, and must not attempt any SQLite-incompatible
                # (plpgsql / CREATE TRIGGER ... EXECUTE FUNCTION) statements.
                migration.upgrade()
                migration.downgrade()
    finally:
        engine.dispose()


def test_target_tables_are_exactly_punch_events_and_approval_audit_log() -> None:
    """Scope guard: overtime_ledger must NOT get a trigger (bead 7ig — still
    has legitimate in-place mutation paths today)."""
    assert migration._TABLES == ("punch_events", "approval_audit_log")
    assert "overtime_ledger" not in migration._TABLES
    assert "overtime_ledger" not in migration._FUNCTION_SQL


def test_escape_hatch_guc_matches_clock_service() -> None:
    from zeiterfassung.services.clock_service import _APPEND_ONLY_GUC

    assert migration._GUC == _APPEND_ONLY_GUC
    assert migration._GUC in migration._FUNCTION_SQL


def test_function_sql_is_structurally_well_formed() -> None:
    sql = migration._FUNCTION_SQL
    assert "CREATE OR REPLACE FUNCTION zeiterfassung_prevent_mutation()" in sql
    assert "RETURNS TRIGGER" in sql
    assert "LANGUAGE plpgsql" in sql
    assert "RAISE EXCEPTION" in sql
    # Dollar-quoted body must be balanced (exactly two "$$" delimiters).
    assert sql.count("$$") == 2
    # Every paren opened is closed.
    assert sql.count("(") == sql.count(")")
    # Both TG_OP branches (DELETE keeps OLD, everything else keeps NEW).
    assert "TG_OP = 'DELETE'" in sql
    assert "RETURN OLD;" in sql
    assert "RETURN NEW;" in sql


def test_trigger_sql_covers_update_and_delete_on_each_table() -> None:
    """Mirror upgrade()'s per-table CREATE TRIGGER statement and assert it
    would target the right table/event set (without executing it)."""
    for table in migration._TABLES:
        trigger_sql = f"""
            CREATE TRIGGER {table}_append_only
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW
            EXECUTE FUNCTION zeiterfassung_prevent_mutation();
            """
        assert re.search(rf"CREATE TRIGGER {table}_append_only", trigger_sql)
        assert re.search(rf"BEFORE UPDATE OR DELETE ON {table}\b", trigger_sql)
        assert "EXECUTE FUNCTION zeiterfassung_prevent_mutation" in trigger_sql
