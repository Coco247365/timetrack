"""Role hierarchy: departments, delegation, scope columns on roles and audit_log.

Revision ID: 0002
Revises: 0001
Create Date: 2026-03-24
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # departments
    # ------------------------------------------------------------------
    op.create_table(
        "departments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("code", sa.String(10), nullable=False),
        sa.Column(
            "teamleiter_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("tenant_id", "code", name="uq_dept_tenant_code"),
    )
    op.create_index("ix_departments_tenant_id", "departments", ["tenant_id"])

    # ------------------------------------------------------------------
    # approval_delegations
    # ------------------------------------------------------------------
    op.create_table(
        "approval_delegations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "delegator_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "delegate_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("valid_from", sa.Date, nullable=False),
        sa.Column("valid_to", sa.Date, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("valid_to >= valid_from", name="ck_delegation_date_order"),
        sa.CheckConstraint("delegator_id != delegate_id", name="ck_delegation_no_self"),
    )
    op.create_index("ix_approval_delegations_tenant_id", "approval_delegations", ["tenant_id"])

    # ------------------------------------------------------------------
    # users — add department_id FK
    # ------------------------------------------------------------------
    op.add_column(
        "users",
        sa.Column(
            "department_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("departments.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_users_department_id", "users", ["department_id"])

    # ------------------------------------------------------------------
    # roles — add data_scope and role_type
    # ------------------------------------------------------------------
    op.add_column(
        "roles",
        sa.Column(
            "data_scope",
            sa.String(20),
            nullable=False,
            server_default="self",
            comment="global | department | self | anonymized_global",
        ),
    )
    op.add_column(
        "roles",
        sa.Column(
            "role_type",
            sa.String(20),
            nullable=True,
            comment="gf | hr | buchhaltung | teamleiter | mitarbeiter | betriebsrat",
        ),
    )

    # ------------------------------------------------------------------
    # audit_log — add data_category and access_reason
    # ------------------------------------------------------------------
    op.add_column(
        "audit_log",
        sa.Column(
            "data_category",
            sa.String(20),
            nullable=True,
            comment="personal | medical | financial | aggregate | system",
        ),
    )
    op.add_column(
        "audit_log",
        sa.Column(
            "access_reason",
            sa.String(500),
            nullable=True,
            comment="Required when data_category = medical",
        ),
    )


def downgrade() -> None:
    # audit_log columns
    op.drop_column("audit_log", "access_reason")
    op.drop_column("audit_log", "data_category")

    # roles columns
    op.drop_column("roles", "role_type")
    op.drop_column("roles", "data_scope")

    # users column
    op.drop_index("ix_users_department_id", table_name="users")
    op.drop_column("users", "department_id")

    # approval_delegations table
    op.drop_index("ix_approval_delegations_tenant_id", table_name="approval_delegations")
    op.drop_table("approval_delegations")

    # departments table
    op.drop_index("ix_departments_tenant_id", table_name="departments")
    op.drop_table("departments")
