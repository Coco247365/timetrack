"""Vertragsstunden + Tagessoll je Wochentag am Arbeitszeit-Profil.

Spec-Nachtrag 14.08. (docs/superpowers/specs/2026-08-14-arbeitszeit-profile-design.md):
NFC-Profile tragen ``contracted_hours_per_week`` (Pflicht bei Neuanlage,
Bestandsprofile bleiben NULL = keine Stundenbindung) und
``weekday_target_minutes`` (JSON ``{"0".."6": Minuten}``, fehlender Tag = 0).
Auto-Profile lassen beide Spalten NULL — ihre Stunden werden aus
``weekday_times`` berechnet.

Revision ID: 0047_profil_vertragsstunden
Revises: 0046_work_time_profiles
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from zeiterfassung.models.base import JSONBCompat

revision: str = "0047_profil_vertragsstunden"
down_revision: str | None = "0046_work_time_profiles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "work_time_profiles",
        sa.Column(
            "contracted_hours_per_week",
            sa.Numeric(5, 2),
            nullable=True,
            comment="Nur kind='nfc'; NULL = Bestandsprofil ohne Stundenbindung",
        ),
    )
    op.add_column(
        "work_time_profiles",
        sa.Column(
            "weekday_target_minutes",
            JSONBCompat(),
            nullable=True,
            comment='Nur kind=\'nfc\': Tages-Soll je Wochentag {"0".."6": Minuten}',
        ),
    )


def downgrade() -> None:
    op.drop_column("work_time_profiles", "weekday_target_minutes")
    op.drop_column("work_time_profiles", "contracted_hours_per_week")
