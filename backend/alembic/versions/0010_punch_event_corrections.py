"""punch_events correction fields

Revision ID: 0010_punch_event_corrections
Revises: 0009_user_birth_date
Create Date: 2026-06-30

Additive: macht das punch_events-Log korrigierbar, ohne Roh-Zeilen zu ändern.
Eine Korrektur ist ein neues Event, das ein früheres ersetzt:
  - corrects_event_id  -> auf welches frühere Event sich die Korrektur bezieht
  - is_void            -> ersetztes Event ersatzlos entfernen
  - created_by         -> Actor (None bei Kiosk-Events)
  - reason             -> Begründung
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_punch_event_corrections"
down_revision: str | None = "0009_user_birth_date"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "punch_events",
        sa.Column("corrects_event_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "punch_events",
        sa.Column("is_void", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "punch_events",
        sa.Column("created_by", sa.UUID(), nullable=True),
    )
    op.add_column(
        "punch_events",
        sa.Column("reason", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_punch_events_corrects_event_id",
        "punch_events",
        ["corrects_event_id"],
    )
    op.create_foreign_key(
        "fk_punch_events_corrects_event_id",
        "punch_events",
        "punch_events",
        ["corrects_event_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_punch_events_created_by",
        "punch_events",
        "users",
        ["created_by"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_punch_events_created_by", "punch_events", type_="foreignkey")
    op.drop_constraint("fk_punch_events_corrects_event_id", "punch_events", type_="foreignkey")
    op.drop_index("ix_punch_events_corrects_event_id", table_name="punch_events")
    op.drop_column("punch_events", "reason")
    op.drop_column("punch_events", "created_by")
    op.drop_column("punch_events", "is_void")
    op.drop_column("punch_events", "corrects_event_id")
