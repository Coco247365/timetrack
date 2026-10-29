"""companies + users.company_id

Revision ID: 0008_companies
Revises: 8493545666bd
Create Date: 2026-06-29

Additive: `companies` lookup (Firmen, per tenant) + `users.company_id` FK.
Companies are an employee grouping, NOT tenants.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import zeiterfassung.models.base

revision: str = "0008_companies"
down_revision: str | None = "8493545666bd"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "companies",
        sa.Column("tenant_id", zeiterfassung.models.base.UUIDCompat(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
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
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_companies_tenant_id"), "companies", ["tenant_id"], unique=False)
    op.add_column(
        "users", sa.Column("company_id", zeiterfassung.models.base.UUIDCompat(), nullable=True)
    )
    op.create_index(op.f("ix_users_company_id"), "users", ["company_id"], unique=False)
    op.create_foreign_key(
        "fk_users_company_id", "users", "companies", ["company_id"], ["id"], ondelete="SET NULL"
    )


def downgrade() -> None:
    op.drop_constraint("fk_users_company_id", "users", type_="foreignkey")
    op.drop_index(op.f("ix_users_company_id"), table_name="users")
    op.drop_column("users", "company_id")
    op.drop_index(op.f("ix_companies_tenant_id"), table_name="companies")
    op.drop_table("companies")
