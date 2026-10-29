"""User personnel fields: hire_date, employment_type, vacation_entitlement_days.

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("hire_date", sa.Date(), nullable=True))
    op.add_column(
        "users",
        sa.Column("employment_type", sa.String(length=10), nullable=False, server_default="hourly"),
    )
    op.add_column(
        "users",
        sa.Column(
            "vacation_entitlement_days", sa.Numeric(4, 1), nullable=False, server_default="30"
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "vacation_entitlement_days")
    op.drop_column("users", "employment_type")
    op.drop_column("users", "hire_date")
