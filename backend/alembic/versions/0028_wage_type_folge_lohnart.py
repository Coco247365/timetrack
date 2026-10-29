"""wage_type_mappings.folge_lohnart — automatische Begleitbuchung

Revision ID: 0028_wage_type_folge
Revises: 0027_user_weekday_target
Create Date: 2026-07-09

Additive: fügt ``folge_lohnart`` (VARCHAR, nullable, internal_code einer
anderen Lohnart) hinzu. Ist sie gesetzt, schreibt der LODAS-Export für jede
Buchung auf die Lohnart automatisch eine zweite Zeile mit gleichem Wert auf
die Folgelohnart — wie im echten Referenz-Export (129 Urlaublohn -> 150 zus.
Urlaubsgeld, 066 Überstunden -> 067 Zuschlag 30%).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028_wage_type_folge"
down_revision: str | None = "0027_user_weekday_target"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "wage_type_mappings",
        sa.Column("folge_lohnart", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("wage_type_mappings", "folge_lohnart")
