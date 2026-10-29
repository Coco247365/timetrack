"""Tabelle sick_mail_reports — Krankmeldungen per E-Mail.

Revision ID: 0021_sick_mail_reports
Revises: 0020_merge_heads
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021_sick_mail_reports"
down_revision: str | None = "0020_merge_heads"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sick_mail_reports",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("message_id", sa.String(512), nullable=False, unique=True),
        sa.Column("sender_email", sa.String(255), nullable=False),
        sa.Column("subject", sa.String(512), nullable=True),
        sa.Column("body_preview", sa.Text(), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("parsed_date_from", sa.Date(), nullable=True),
        sa.Column("parsed_date_to", sa.Date(), nullable=True),
        sa.Column("parsed_hint", sa.String(255), nullable=True),
        sa.Column(
            "employee_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column(
            "absence_request_id",
            sa.Uuid(),
            sa.ForeignKey("approval_requests.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default="NEW", index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("sick_mail_reports")
