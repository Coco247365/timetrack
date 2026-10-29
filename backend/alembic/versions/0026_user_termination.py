"""users.termination_date + termination_reason — Austritt

Revision ID: 0026_user_termination
Revises: 0025_sick_status
Create Date: 2026-07-08

Additive: fügt ``termination_date`` (Date) und ``termination_reason`` (VARCHAR)
zu ``users`` hinzu. Beide nullable — nur bei ausgetretenen Mitarbeitern gesetzt.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0026_user_termination"
down_revision: str | None = "0025_sick_status"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("termination_date", sa.Date(), nullable=True))
    op.add_column("users", sa.Column("termination_reason", sa.String(length=40), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "termination_reason")
    op.drop_column("users", "termination_date")
