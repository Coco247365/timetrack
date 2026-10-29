"""Monatlicher Stundenzettel-Versand: User-Flag + Versandprotokoll.

Revision ID: 0043_timesheet_mail
Revises: 0042_event_source_auto_timeout
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from zeiterfassung.models.base import UUIDCompat

revision: str = "0043_timesheet_mail"
down_revision: str | None = "0042_event_source_auto_timeout"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "monthly_timesheet_email",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.create_table(
        "timesheet_mail_log",
        sa.Column("id", UUIDCompat(), primary_key=True),
        sa.Column(
            "tenant_id",
            UUIDCompat(),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "employee_id",
            UUIDCompat(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("year_month", sa.Date(), nullable=False),
        sa.Column("sent_to", sa.String(320), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("detail", sa.String(500), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_timesheet_mail_log_tenant_id", "timesheet_mail_log", ["tenant_id"])
    op.create_index("ix_timesheet_mail_log_employee_id", "timesheet_mail_log", ["employee_id"])


def downgrade() -> None:
    op.drop_index("ix_timesheet_mail_log_employee_id", table_name="timesheet_mail_log")
    op.drop_index("ix_timesheet_mail_log_tenant_id", table_name="timesheet_mail_log")
    op.drop_table("timesheet_mail_log")
    op.drop_column("users", "monthly_timesheet_email")
