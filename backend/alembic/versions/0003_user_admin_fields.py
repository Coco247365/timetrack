"""User admin fields: manager_id, time_capture, auto_hours_per_day.

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("manager_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_users_manager_id",
        "users",
        "users",
        ["manager_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column(
        "users",
        sa.Column(
            "time_capture",
            sa.String(length=10),
            nullable=False,
            server_default="nfc",
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "auto_hours_per_day",
            sa.Numeric(4, 2),
            nullable=False,
            server_default="8.0",
        ),
    )


def downgrade() -> None:
    op.drop_constraint("fk_users_manager_id", "users", type_="foreignkey")
    op.drop_column("users", "auto_hours_per_day")
    op.drop_column("users", "time_capture")
    op.drop_column("users", "manager_id")
