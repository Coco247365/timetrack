"""payroll_bookings.booking_date

Revision ID: 0013_payroll_booking_date
Revises: 0012_payroll_bookings
Create Date: 2026-07-01

Additive: optionales Doku-Datum an manuellen Lohnbuchungen (nicht im Export).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_payroll_booking_date"
down_revision: str | None = "0012_payroll_bookings"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("payroll_bookings", sa.Column("booking_date", sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column("payroll_bookings", "booking_date")
