"""overtime: user payout settings + overtime_ledger

Revision ID: 0014_overtime
Revises: 0013_payroll_booking_date
Create Date: 2026-07-01

Additive:
- users.overtime_payout_mode (none|all|above_cap), users.overtime_cap_hours
- overtime_ledger (datiertes, append-only Überstunden-Log)
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_overtime"
down_revision: str | None = "0013_payroll_booking_date"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "overtime_payout_mode",
            sa.String(length=16),
            nullable=False,
            server_default="above_cap",
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "overtime_cap_hours",
            sa.Numeric(5, 1),
            nullable=False,
            server_default="20",
        ),
    )
    op.create_table(
        "overtime_ledger",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("employee_id", sa.UUID(), nullable=False),
        sa.Column("year_month", sa.Date(), nullable=False),
        sa.Column("entry_date", sa.Date(), nullable=False),
        sa.Column("entry_type", sa.String(length=12), nullable=False),
        sa.Column("hours", sa.Numeric(7, 2), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["employee_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_overtime_ledger_tenant_id", "overtime_ledger", ["tenant_id"])
    op.create_index("ix_overtime_ledger_employee_id", "overtime_ledger", ["employee_id"])
    op.create_index("ix_overtime_ledger_year_month", "overtime_ledger", ["year_month"])


def downgrade() -> None:
    op.drop_table("overtime_ledger")
    op.drop_column("users", "overtime_cap_hours")
    op.drop_column("users", "overtime_payout_mode")
