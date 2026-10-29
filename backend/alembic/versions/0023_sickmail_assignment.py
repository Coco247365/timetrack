"""sick_mail_reports.assignment — automatische vs. manuelle Zuordnung.

Revision ID: 0023_sickmail_assignment
Revises: 0022_merge_heads
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023_sickmail_assignment"
down_revision: str | None = "0022_merge_heads"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sick_mail_reports", sa.Column("assignment", sa.String(16), nullable=True))
    # Bestehende zugeordnete Eintraege: Herkunft unbekannt -> als automatisch
    # annehmen (bisher entstanden fast alle durch den Absender-Match).
    op.execute("UPDATE sick_mail_reports SET assignment='auto' WHERE status='LINKED'")


def downgrade() -> None:
    op.drop_column("sick_mail_reports", "assignment")
