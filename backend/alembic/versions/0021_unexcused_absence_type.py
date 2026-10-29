"""Add RequestType.UNEXCUSED_ABSENCE (unentschuldigtes Fehlen).

Ein manuell eintragbarer Abwesenheitstyp, der als Minus ins Arbeitszeitkonto zählt
(im Gegensatz zu Urlaub/Krank, die das Soll abdecken).

Postgres: neuer Wert im nativen Enum ``request_type_enum`` — ``ALTER TYPE ... ADD VALUE``
läuft außerhalb der Migrations-Transaktion (autocommit_block). Für SQLite (Tests) ist der
Typ ein VARCHAR/CHECK und braucht nichts.

Revision ID: 0021_unexcused_absence_type
Revises: 0020_merge_heads
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0021_unexcused_absence_type"
down_revision: str | None = "0020_merge_heads"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE request_type_enum ADD VALUE IF NOT EXISTS 'UNEXCUSED_ABSENCE'")


def downgrade() -> None:
    # Postgres kann Enum-Werte nicht entfernen — bewusst No-op.
    pass
