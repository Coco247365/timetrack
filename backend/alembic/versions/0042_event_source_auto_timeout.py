"""event_source_enum: SYSTEM_AUTO_TIMEOUT (Auto-Ausstempeln nach 14 h).

Revision ID: 0042_event_source_auto_timeout
Revises: 0041_user_email_nullable
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0042_event_source_auto_timeout"
down_revision: str | None = "0041_user_email_nullable"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Neuer Quelle-Wert für automatisch (nach settings.auto_clockout_hours)
    # beendete Schichten. ALTER TYPE ... ADD VALUE kann nicht in einer
    # Transaktion laufen -> autocommit_block.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE event_source_enum ADD VALUE IF NOT EXISTS 'SYSTEM_AUTO_TIMEOUT'")


def downgrade() -> None:
    # Enum-Werte lassen sich in PostgreSQL nicht ohne Weiteres entfernen; ein
    # ungenutzter zusätzlicher Wert ist harmlos. Bewusst No-op.
    pass
