"""overtime_ledger.hours: Numeric(7,2) -> Numeric(9,4).

Zwei Nachkommastellen sind ein 0,6-Minuten-Raster — eine echte Minute
(1/60 h) ist darin nicht darstellbar. Beim Umrechnen in h:min kippte die
Anzeige deshalb um ±1 Minute gegenueber dem Altsystem-Sollwert (z. B.
33:51 -> 33:50). Vier Nachkommastellen stellen Minuten bis auf 0,002 min
genau dar; bestehende Werte bleiben unveraendert (nur breiter).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0049_overtime_hours_minutengenau"
down_revision = "0048_overtime_payout_forfeit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "overtime_ledger",
        "hours",
        existing_type=sa.Numeric(7, 2),
        type_=sa.Numeric(9, 4),
        existing_nullable=False,
    )


def downgrade() -> None:
    # Rundet auf Hundertstel zurueck — die Minute-Genauigkeit geht dabei
    # verloren, mehr Verlust entsteht nicht.
    op.alter_column(
        "overtime_ledger",
        "hours",
        existing_type=sa.Numeric(9, 4),
        type_=sa.Numeric(7, 2),
        existing_nullable=False,
    )
