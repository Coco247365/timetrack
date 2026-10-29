"""accounts.token_version — sofortige Access-Token-Entwertung (login-hardening).

Revision ID: 0037_account_token_version
Revises: 0036_append_only_triggers
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0037_account_token_version"
down_revision: str | None = "0036_append_only_triggers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column("token_version", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("accounts", "token_version")
