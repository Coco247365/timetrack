"""users.weekday_target_minutes — wochentagsspezifisches Tages-Soll

Revision ID: 0027_user_weekday_target
Revises: 0026_user_termination
Create Date: 2026-07-09

Additive: fügt ``weekday_target_minutes`` (JSONB, nullable) zu ``users`` hinzu.
Eine Liste [Mo..So] mit dem Tages-Soll je Wochentag in Minuten. NULL = Fallback
auf ``contracted_hours_per_week`` ÷ 5 (Mo–Fr), 0 (Sa/So) — bisheriges Verhalten.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0027_user_weekday_target"
down_revision: str | None = "0026_user_termination"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "weekday_target_minutes",
            postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "weekday_target_minutes")
