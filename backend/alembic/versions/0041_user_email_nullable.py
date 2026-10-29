"""users.email nullable — Mitarbeiter ohne Firmen-E-Mail anlegen koennen.

Revision ID: 0041_user_email_nullable
Revises: 0040_kiosk_message_active_from
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0041_user_email_nullable"
down_revision: str | None = "0040_kiosk_message_active_from"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("users", "email", existing_type=sa.String(320), nullable=True)


def downgrade() -> None:
    # Zurueck auf NOT NULL: leere Adressen mit einem eindeutigen Platzhalter
    # fuellen, sonst schlaegt die Constraint fehl.
    op.execute("UPDATE users SET email = 'noemail-' || id || '@invalid' WHERE email IS NULL")
    op.alter_column("users", "email", existing_type=sa.String(320), nullable=False)
