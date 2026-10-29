"""wage_type_mappings.datev_lohnart nullable (Katalog ohne Nummer)

Revision ID: 0017_wage_type_nullable
Revises: 0016_absence_day_part
Create Date: 2026-07-02

Der Lohnarten-Katalog kennt jetzt Zeilen OHNE DATEV-Nummer: die Kategorie
existiert (aus der Registry), aber der Nutzer hat noch keine Nummer vergeben.
Exportiert wird nur, was eine Nummer hat — deshalb darf die Spalte NULL sein.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_wage_type_nullable"
down_revision: str | None = "0016_absence_day_part"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "wage_type_mappings",
        "datev_lohnart",
        existing_type=sa.Integer(),
        nullable=True,
    )


def downgrade() -> None:
    # Zeilen ohne Nummer können im alten Schema nicht existieren -> entfernen.
    op.execute("DELETE FROM wage_type_mappings WHERE datev_lohnart IS NULL")
    op.alter_column(
        "wage_type_mappings",
        "datev_lohnart",
        existing_type=sa.Integer(),
        nullable=False,
    )
