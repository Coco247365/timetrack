"""users.wage_profile_id — Abrechnungsprofil pro Mitarbeiter.

Revision ID: 0024_user_wage_profile
Revises: 0023_sickmail_assignment
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024_user_wage_profile"
down_revision: str | None = "0023_sickmail_assignment"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("wage_profile_id", sa.String(36), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "wage_profile_id")
