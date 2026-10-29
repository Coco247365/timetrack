"""Merge der parallelen Migrationskoepfe (Runde 5).

Nach dem Merge von master (PRs #103/#105) in feat/sam-go-live existieren
zwei Koepfe: 0032_device_location (SAM/Device-Auth-Kette) und
0033_weekend_booking (Team-Hauptkette, haengt an 0032_export_file_name).
Beide zweigen unabhaengig von 0030_user_initial_vacation ab. Dieser leere
Merge fuehrt die Historie wieder auf einen Kopf zusammen, damit
`alembic upgrade head` eindeutig bleibt.

Revision ID: 0033_merge_heads
Revises: 0032_device_location, 0033_weekend_booking
"""

from collections.abc import Sequence

revision: str = "0033_merge_heads"
down_revision: str | Sequence[str] | None = (
    "0032_device_location",
    "0033_weekend_booking",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
