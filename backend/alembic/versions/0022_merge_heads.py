"""Merge der parallelen Migrationskoepfe (Runde 2).

0021_sick_mail_reports (Krankmeldung per E-Mail) und 0021_unexcused_absence_type
(unentschuldigtes Fehlen) sind unabhaengig voneinander auf 0020 aufgesetzt worden —
dieser leere Merge fuehrt die Historie wieder auf einen Kopf zusammen.

Revision ID: 0022_merge_heads
Revises: 0021_sick_mail_reports, 0021_unexcused_absence_type
"""

from collections.abc import Sequence

revision: str = "0022_merge_heads"
down_revision: str | Sequence[str] | None = (
    "0021_sick_mail_reports",
    "0021_unexcused_absence_type",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
