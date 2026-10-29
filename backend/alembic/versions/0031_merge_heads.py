"""Merge der parallelen Migrationskoepfe (Runde 4).

0019_device_auth (avd-Branch: NFC-Terminal-Device-Auth) und
0030_user_initial_vacation (Team-Hauptkette) wurden unabhaengig voneinander
gemergt und haben seither zwei Koepfe — dieser leere Merge fuehrt die
Historie wieder auf einen Kopf zusammen, damit `alembic upgrade head`
eindeutig ist (praeexistierender Defekt aus dem avd-Merge, siehe
zeiterfassung-2fn Part 0).

Revision ID: 0031_merge_heads
Revises: 0019_device_auth, 0030_user_initial_vacation
"""

from collections.abc import Sequence

revision: str = "0031_merge_heads"
down_revision: str | Sequence[str] | None = (
    "0019_device_auth",
    "0030_user_initial_vacation",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
