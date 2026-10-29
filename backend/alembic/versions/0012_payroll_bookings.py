"""wage_type_mappings.unit + payroll_bookings

Revision ID: 0012_payroll_bookings
Revises: 0011_company_datev
Create Date: 2026-07-01

Additive:
- wage_type_mappings.unit (hours | amount | days)
- payroll_bookings (manuelle Lohnart-Buchungen: Pauschalen/Beträge/Sonderzahlungen)
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_payroll_bookings"
down_revision: str | None = "0011_company_datev"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "wage_type_mappings",
        sa.Column("unit", sa.String(length=10), nullable=False, server_default="hours"),
    )
    # Bestehende Betrags-Lohnarten sinnvoll vorbelegen.
    op.execute("UPDATE wage_type_mappings SET unit = 'amount' WHERE internal_code LIKE '%BETRAG'")
    op.create_table(
        "payroll_bookings",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("employee_id", sa.UUID(), nullable=False),
        sa.Column("year_month", sa.Date(), nullable=False),
        sa.Column("wage_type_code", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Numeric(10, 2), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["employee_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_payroll_bookings_tenant_id", "payroll_bookings", ["tenant_id"])
    op.create_index("ix_payroll_bookings_employee_id", "payroll_bookings", ["employee_id"])
    op.create_index("ix_payroll_bookings_year_month", "payroll_bookings", ["year_month"])


def downgrade() -> None:
    op.drop_table("payroll_bookings")
    op.drop_column("wage_type_mappings", "unit")
