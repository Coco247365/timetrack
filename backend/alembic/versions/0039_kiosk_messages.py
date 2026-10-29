"""kiosk_messages — Kurzmeldungen auf dem Terminal (#189).

Revision ID: 0039_kiosk_messages
Revises: 0038_punch_created_by_account
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from zeiterfassung.models.base import UUIDCompat

revision: str = "0039_kiosk_messages"
down_revision: str | None = "0038_punch_created_by_account"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kiosk_messages",
        sa.Column("id", UUIDCompat(), primary_key=True),
        sa.Column(
            "tenant_id",
            UUIDCompat(),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "employee_id",
            UUIDCompat(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
            index=True,
        ),
        sa.Column("text", sa.String(length=280), nullable=False),
        sa.Column("active_until", sa.Date(), nullable=True),
        sa.Column(
            "created_by_account_id",
            UUIDCompat(),
            sa.ForeignKey("accounts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_table("kiosk_messages")
