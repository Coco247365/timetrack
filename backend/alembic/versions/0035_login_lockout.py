"""login lockout — failed_login_count + locked_until on accounts (fkw.4).

Additive: zwei neue Spalten auf ``accounts``. DB-basiertes Lockout (kein Redis):
10 Fehlversuche sperren den Account 5 Minuten (siehe user_service.authenticate).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0035_login_lockout"
down_revision: str | None = "0034_refresh_sessions"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column("failed_login_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "accounts",
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("accounts", "locked_until")
    op.drop_column("accounts", "failed_login_count")
