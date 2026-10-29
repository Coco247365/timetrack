"""append_only_triggers — DB-enforced append-only for punch_events + approval_audit_log.

BetrVG §87: raw punch events and the approval audit log must stay
append-only — corrections are new rows (``corrects_event_id`` / a new audit
row), never an in-place ``UPDATE``/``DELETE``. That invariant has so far been
convention-only (see ``.claude/rules/append-only-ledger-and-events.md``); this
migration enforces it at the Postgres layer with ``BEFORE UPDATE OR DELETE``
triggers that ``RAISE EXCEPTION``.

Postgres-only: SQLite (used for the whole test suite) has no trigger support
worth mirroring here, and the app never talks to SQLite in production, so
``upgrade()``/``downgrade()`` are a no-op on any non-postgres dialect. This
means the trigger enforcement itself is **not exercised by the SQLite test
suite** — it is a known coverage gap, verified manually against Postgres only
(``uv run alembic upgrade head`` against a local Postgres + a manual
``UPDATE``/``DELETE`` that is expected to raise). ``tests/test_append_only_migration.py``
covers what SQLite *can* verify: the migration module compiles, exposes the
expected SQL identifiers, and is a no-op on the SQLite dialect.

Escape hatch: one legitimate delete path exists —
``clock_service.undo_last_punch`` (NFC-terminal mis-tap recovery, 90s grace
window). It sets the session GUC
``zeiterfassung.allow_append_only_mutation = 'on'`` via ``SET LOCAL`` before
its ``DELETE``, which both triggers check for and allow. Nothing else may set
this GUC.

Deliberately excludes ``overtime_ledger``: ``overtime_service`` still performs
legitimate in-place UPDATE/DELETE on ledger adjustments today (tracked
separately, bead 7ig). A trigger there would break those endpoints — it needs
its own migration once that code path is converted to append-only.

Revision ID: 0036_append_only_triggers
Revises: 0035_login_lockout
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0036_append_only_triggers"
down_revision: str | None = "0035_login_lockout"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_GUC = "zeiterfassung.allow_append_only_mutation"

_FUNCTION_SQL = f"""
CREATE OR REPLACE FUNCTION zeiterfassung_prevent_mutation()
RETURNS TRIGGER AS $$
BEGIN
    IF current_setting('{_GUC}', true) = 'on' THEN
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END IF;
    RAISE EXCEPTION
        '% is append-only: % on %.% is not allowed (BetrVG §87 — corrections must be new rows). '
        'Set "{_GUC}" = ''on'' for the one sanctioned exception (clock_service.undo_last_punch).',
        TG_TABLE_NAME, TG_OP, TG_TABLE_SCHEMA, TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;
"""

_TABLES = ("punch_events", "approval_audit_log")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute(_FUNCTION_SQL)
    for table in _TABLES:
        op.execute(
            f"""
            CREATE TRIGGER {table}_append_only
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW
            EXECUTE FUNCTION zeiterfassung_prevent_mutation();
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    for table in _TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table};")
    op.execute("DROP FUNCTION IF EXISTS zeiterfassung_prevent_mutation();")
