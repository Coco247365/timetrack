"""accounts and account_roles

Revision ID: 8493545666bd
Revises: 0006
Create Date: 2026-06-29

Additive: adds `accounts` (login identities, separate from employees in `users`)
and `account_roles` (account ↔ role). Does not change existing tables.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import zeiterfassung.models.base

# revision identifiers, used by Alembic.
revision: str = "8493545666bd"
down_revision: str | None = "0006"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "accounts",
        sa.Column("tenant_id", zeiterfassung.models.base.UUIDCompat(), nullable=True),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("hashed_password", sa.String(length=255), nullable=False),
        sa.Column("is_superadmin", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("employee_id", zeiterfassung.models.base.UUIDCompat(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("id", zeiterfassung.models.base.UUIDCompat(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["employee_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_accounts_email"), "accounts", ["email"], unique=False)
    op.create_index(op.f("ix_accounts_employee_id"), "accounts", ["employee_id"], unique=False)
    op.create_index(op.f("ix_accounts_tenant_id"), "accounts", ["tenant_id"], unique=False)
    op.create_table(
        "account_roles",
        sa.Column("account_id", zeiterfassung.models.base.UUIDCompat(), nullable=False),
        sa.Column("role_id", zeiterfassung.models.base.UUIDCompat(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["role_id"], ["roles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("account_id", "role_id"),
    )


def downgrade() -> None:
    op.drop_table("account_roles")
    op.drop_index(op.f("ix_accounts_tenant_id"), table_name="accounts")
    op.drop_index(op.f("ix_accounts_employee_id"), table_name="accounts")
    op.drop_index(op.f("ix_accounts_email"), table_name="accounts")
    op.drop_table("accounts")
