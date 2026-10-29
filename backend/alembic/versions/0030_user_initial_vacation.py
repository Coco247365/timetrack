"""users.initial_vacation_remaining_days — Resturlaub im Eintrittsjahr

Revision ID: 0030_user_initial_vacation
Revises: 0029_merge_heads
Create Date: 2026-07-09

Additive: fügt ``initial_vacation_remaining_days`` (Numeric, nullable) zu ``users``
hinzu. Im Jahr des ``hire_date`` bildet dieser Wert die Urlaubs-Bilanz-Basis
(statt des vollen Jahresanspruchs). NULL = kein Sonderwert (bisheriges Verhalten).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0030_user_initial_vacation"
down_revision: str | None = "0029_merge_heads"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("initial_vacation_remaining_days", sa.Numeric(4, 1), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "initial_vacation_remaining_days")
