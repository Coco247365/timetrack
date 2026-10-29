"""daily_timesheets.break_auto_override_minutes: HR-Korrektur des Pausen-Aufschlags.

Die betriebliche Pausenregel (> 4 h -> 45 min) ergaenzt fehlende Pausenminuten
automatisch. HR kann den Aufschlag pro Tag nach oben oder unten korrigieren —
der Wert ist das DELTA zu den gestempelten Pausen (>= 0, nie negativ) und
ersetzt die Automatik fuer diesen Tag. NULL = Automatik.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0050_break_auto_override"
down_revision = "0049_overtime_hours_minutengenau"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "daily_timesheets",
        sa.Column("break_auto_override_minutes", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("daily_timesheets", "break_auto_override_minutes")
