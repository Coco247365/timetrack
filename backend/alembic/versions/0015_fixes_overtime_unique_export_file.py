"""Fixes: overtime_ledger Unique-Indexe + payroll_exports.file_content

Revision ID: 0015_fixes
Revises: 0014_overtime
Create Date: 2026-07-02

- Partial-Unique-Indexe verhindern doppelte Ansammlung/Auszahlung pro
  (tenant, employee, monat) bei parallelen Export-Läufen (Race).
- payroll_exports.file_content speichert die erzeugte LODAS-Datei, damit der
  Download exakt das Original liefert (kein Re-Generate).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_fixes"
down_revision: str | None = "0014_overtime"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_overtime_accrual_per_month",
        "overtime_ledger",
        ["tenant_id", "employee_id", "year_month"],
        unique=True,
        postgresql_where=sa.text("entry_type = 'accrual'"),
        sqlite_where=sa.text("entry_type = 'accrual'"),
    )
    op.create_index(
        "uq_overtime_payout_per_month",
        "overtime_ledger",
        ["tenant_id", "employee_id", "year_month"],
        unique=True,
        postgresql_where=sa.text("entry_type = 'payout'"),
        sqlite_where=sa.text("entry_type = 'payout'"),
    )
    op.add_column("payroll_exports", sa.Column("file_content", sa.LargeBinary(), nullable=True))


def downgrade() -> None:
    op.drop_column("payroll_exports", "file_content")
    op.drop_index("uq_overtime_payout_per_month", table_name="overtime_ledger")
    op.drop_index("uq_overtime_accrual_per_month", table_name="overtime_ledger")
