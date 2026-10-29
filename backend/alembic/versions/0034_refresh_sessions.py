"""refresh_sessions — Refresh-Token-Rotation + Reuse-Detection (fkw.2).

Additive: neue Tabelle ``refresh_sessions``, eine Zeile pro ausgestellter
Refresh-Token-JTI. Aendert keine bestehenden Tabellen.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import zeiterfassung.models.base

# revision identifiers, used by Alembic.
revision: str = "0034_refresh_sessions"
down_revision: str | None = "0033_merge_heads"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "refresh_sessions",
        sa.Column("id", zeiterfassung.models.base.UUIDCompat(), nullable=False),
        sa.Column("jti", sa.String(length=64), nullable=False),
        sa.Column("account_id", zeiterfassung.models.base.UUIDCompat(), nullable=False),
        sa.Column("tenant_id", zeiterfassung.models.base.UUIDCompat(), nullable=True),
        sa.Column("family_id", zeiterfassung.models.base.UUIDCompat(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rotated_to_jti", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_refresh_sessions_jti"), "refresh_sessions", ["jti"], unique=True)
    op.create_index(
        op.f("ix_refresh_sessions_account_id"), "refresh_sessions", ["account_id"], unique=False
    )
    op.create_index(
        op.f("ix_refresh_sessions_tenant_id"), "refresh_sessions", ["tenant_id"], unique=False
    )
    op.create_index(
        op.f("ix_refresh_sessions_family_id"), "refresh_sessions", ["family_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_refresh_sessions_family_id"), table_name="refresh_sessions")
    op.drop_index(op.f("ix_refresh_sessions_tenant_id"), table_name="refresh_sessions")
    op.drop_index(op.f("ix_refresh_sessions_account_id"), table_name="refresh_sessions")
    op.drop_index(op.f("ix_refresh_sessions_jti"), table_name="refresh_sessions")
    op.drop_table("refresh_sessions")
