"""absence: day_part (Halbtag) an approval_requests

Revision ID: 0016_absence_day_part
Revises: 0015_fixes
Create Date: 2026-07-02

Additive:
- approval_requests.day_part (FULL|AM|PM), default FULL — ermöglicht halbe
  Urlaubstage. Bestandsdaten werden via server_default auf FULL gesetzt.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_absence_day_part"
down_revision: str | None = "0015_fixes"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "approval_requests",
        sa.Column(
            "day_part",
            sa.String(length=4),
            nullable=False,
            server_default="FULL",
        ),
    )


def downgrade() -> None:
    op.drop_column("approval_requests", "day_part")
