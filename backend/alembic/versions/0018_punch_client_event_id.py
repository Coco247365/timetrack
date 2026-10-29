"""Add client_event_id to punch_events (offline-queue idempotency).

Terminals with an offline buffer re-send punches after a network outage.
The client_event_id (generated on the device) makes retries idempotent:
a partial unique index rejects the same event being booked twice.

Revision ID: 0018_punch_client_event_id
Revises: 0017_wage_type_nullable
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018_punch_client_event_id"
down_revision: str | None = "0017_wage_type_nullable"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "punch_events",
        sa.Column("client_event_id", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "uq_punch_events_tenant_client_event",
        "punch_events",
        ["tenant_id", "client_event_id"],
        unique=True,
        postgresql_where=sa.text("client_event_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_punch_events_tenant_client_event", table_name="punch_events")
    op.drop_column("punch_events", "client_event_id")
