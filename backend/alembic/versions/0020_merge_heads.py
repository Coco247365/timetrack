"""Merge der parallelen Migrationskoepfe.

0019_account_scope_departments (zustaendige Bereiche pro Konto) und
0019_counting_start_date (Erfassungsbeginn) sind unabhaengig voneinander
auf 0018 aufgesetzt worden — dieser leere Merge fuehrt die Historie wieder
auf einen Kopf zusammen, damit `alembic upgrade head` eindeutig ist.

Revision ID: 0020_merge_heads
Revises: 0019_account_scope_departments, 0019_counting_start_date
"""

from collections.abc import Sequence

revision: str = "0020_merge_heads"
down_revision: str | Sequence[str] | None = (
    "0019_account_scope_departments",
    "0019_counting_start_date",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
