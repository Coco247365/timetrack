"""request_type_enum: neuer Wert OVERTIME_REDUCTION (Spec 2026-08-11).

Revision ID: 0044_overtime_reduction_enum
Revises: 0043_timesheet_mail
"""

from __future__ import annotations

from alembic import op

revision: str = "0044_overtime_reduction_enum"
down_revision: str | None = "0043_timesheet_mail"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE darf in PG nicht in einer Transaktion laufen.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE request_type_enum ADD VALUE IF NOT EXISTS 'OVERTIME_REDUCTION'")


def downgrade() -> None:
    # PostgreSQL kann Enum-Werte nicht entfernen — bewusstes No-Op.
    pass
