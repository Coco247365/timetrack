"""approval_requests.sick_status — Art der Krankmeldung (AU/LVZ)

Revision ID: 0025_sick_status
Revises: 0024_user_wage_profile
Create Date: 2026-07-08

Additive: fügt eine nullable Spalte ``sick_status`` (VARCHAR, native_enum=False)
zu ``approval_requests`` hinzu. Nur für SICK_LEAVE-Anträge gesetzt.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0025_sick_status"
down_revision: str | None = "0024_user_wage_profile"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "approval_requests",
        sa.Column("sick_status", sa.String(length=20), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("approval_requests", "sick_status")
