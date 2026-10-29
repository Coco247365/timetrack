"""kiosk_messages.active_from — Meldungen im Voraus planen (#189).

Revision ID: 0040_kiosk_message_active_from
Revises: 0039_kiosk_messages
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0040_kiosk_message_active_from"
down_revision: str | None = "0039_kiosk_messages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("kiosk_messages", sa.Column("active_from", sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column("kiosk_messages", "active_from")
