"""devices.location — optionaler Standort eines NFC-Terminals

Revision ID: 0032_device_location
Revises: 0031_merge_heads
Create Date: 2026-07-13

Additive: fuegt ``location`` (String(255), nullable) zu ``devices`` hinzu, damit
Terminals bei der Registrierung mit einem menschenlesbaren Standort (z. B.
"Halle 2 / Eingang") versehen werden koennen. NULL = kein Standort hinterlegt
(bisheriges Verhalten).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032_device_location"
down_revision: str | None = "0031_merge_heads"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("devices", sa.Column("location", sa.String(255), nullable=True))


def downgrade() -> None:
    op.drop_column("devices", "location")
