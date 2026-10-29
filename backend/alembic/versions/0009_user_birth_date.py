"""users.birth_date

Revision ID: 0009_user_birth_date
Revises: 0008_companies
Create Date: 2026-06-29

Additive: adds `users.birth_date` (Geburtstag).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_user_birth_date"
down_revision: str | None = "0008_companies"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("birth_date", sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "birth_date")
