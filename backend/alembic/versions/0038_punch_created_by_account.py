"""punch_events.created_by_account_id — welches Login-Konto die Korrektur anlegte.

``created_by`` (verknüpfter Mitarbeiter) bleibt; neu kommt das ausführende
Konto dazu, damit auch Admin-Konten ohne Mitarbeiter-Verknüpfung im
Stempel-Protokoll attributierbar sind (#169). Alt-Einträge bleiben NULL.

Revision ID: 0038_punch_created_by_account
Revises: 0037_account_token_version
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from zeiterfassung.models.base import UUIDCompat

# revision identifiers, used by Alembic.
revision: str = "0038_punch_created_by_account"
down_revision: str | None = "0037_account_token_version"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "punch_events",
        sa.Column(
            "created_by_account_id",
            UUIDCompat(),
            sa.ForeignKey("accounts.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("punch_events", "created_by_account_id")
