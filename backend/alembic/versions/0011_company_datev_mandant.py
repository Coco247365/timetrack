"""companies DATEV-Mandant (advisor_number, client_number)

Revision ID: 0011_company_datev
Revises: 0010_punch_event_corrections
Create Date: 2026-06-30

Additive: adds DATEV Berater-Nr. + Mandanten-Nr. per company.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_company_datev"
down_revision: str | None = "0010_punch_event_corrections"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("companies", sa.Column("advisor_number", sa.String(length=20), nullable=True))
    op.add_column("companies", sa.Column("client_number", sa.String(length=20), nullable=True))


def downgrade() -> None:
    op.drop_column("companies", "client_number")
    op.drop_column("companies", "advisor_number")
