"""Add accounts.scope_department_ids (zustaendige Bereiche pro Konto).

Optionale Liste von Department-UUIDs am Login-Konto. Wenn gesetzt, gelten
diese Bereiche fuer alle Bereichs-Rechte (Urlaub genehmigen, Team-Sicht,
Teamkalender) statt der Abteilung der verknuepften Personalakte.

Revision ID: 0019_account_scope_departments
Revises: 0018_punch_client_event_id
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_account_scope_departments"
down_revision: str | None = "0018_punch_client_event_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column("scope_department_ids", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("accounts", "scope_department_ids")
