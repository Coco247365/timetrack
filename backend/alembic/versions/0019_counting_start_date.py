"""Add users.counting_start_date (Erfassungsbeginn) — getrennt vom Firmeneintritt.

``hire_date`` bleibt der echte Firmeneintritt (Dienstalter, Firmenjubilaeum).
``counting_start_date`` ist der Erfassungsbeginn: ab hier zaehlen Ueberstunden,
unentschuldigtes Fehlen und das Arbeitszeitkonto.

Backfill: bestehende Mitarbeiter bekommen den Erfassungsbeginn 2026-07-01
(Tag, an dem das System scharf gestellt wurde) — davor gibt es keine echten Daten.

Revision ID: 0019_counting_start_date
Revises: 0018_punch_client_event_id
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_counting_start_date"
down_revision: str | None = "0018_punch_client_event_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("counting_start_date", sa.Date(), nullable=True),
    )
    # Bestand scharf stellen: Erfassung zaehlt fuer alle ab Systemstart 01.07.2026.
    op.execute(
        "UPDATE users SET counting_start_date = DATE '2026-07-01' WHERE counting_start_date IS NULL"
    )


def downgrade() -> None:
    op.drop_column("users", "counting_start_date")
