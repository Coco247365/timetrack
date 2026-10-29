"""daily_timesheets.weekend_booking — Wochenend-/Feiertagsarbeit als Gleitzeit oder Überstunden.

NULL = Überstunden (Default: zählt nicht ins Konto, wird direkt vergütet),
"gleitzeit" = zählt ins Überstundenkonto.
"""

import sqlalchemy as sa
from alembic import op

revision = "0033_weekend_booking"
down_revision = "0032_export_file_name"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "daily_timesheets",
        sa.Column("weekend_booking", sa.String(length=16), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("daily_timesheets", "weekend_booking")
