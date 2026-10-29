"""Merge der parallelen Migrationskoepfe (Runde 3).

0028_lvz_to_lfz (LFZ-Rename) und 0028_wage_type_folge (Folgelohnarten) sind
unabhaengig voneinander auf 0027 aufgesetzt worden — dieser leere Merge fuehrt
die Historie wieder auf einen Kopf zusammen, damit `alembic upgrade head`
eindeutig ist (der doppelte Kopf hat das Deploy gestoppt).

Revision ID: 0029_merge_heads
Revises: 0028_lvz_to_lfz, 0028_wage_type_folge
"""

from collections.abc import Sequence

revision: str = "0029_merge_heads"
down_revision: str | Sequence[str] | None = (
    "0028_lvz_to_lfz",
    "0028_wage_type_folge",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
