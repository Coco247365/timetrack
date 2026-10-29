"""User employment_status (active|paused|deleted).

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "employment_status", sa.String(length=10), nullable=False, server_default="active"
        ),
    )
    # Bestehende inaktive Mitarbeiter als 'paused' markieren (nicht gelöscht).
    op.execute("UPDATE users SET employment_status = 'paused' WHERE is_active = false")


def downgrade() -> None:
    op.drop_column("users", "employment_status")
