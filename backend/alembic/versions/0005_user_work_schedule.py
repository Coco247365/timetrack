"""User work_schedule_id (zugewiesenes Arbeitszeitmodell).

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("work_schedule_id", sa.String(length=36), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "work_schedule_id")
