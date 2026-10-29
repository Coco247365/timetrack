"""overtime_payout_mode auf 24 Zeichen (neuer Wert 'forfeit_above_cap').

Altsystem kappt das Gleitzeitkonto bei 20 h und laesst den Ueberlauf VERFALLEN.
Unser bisheriger Modus ``above_cap`` zahlt ihn stattdessen aus — beim ersten
Monatsabschluss nach der Uebernahme waere das bares Geld fuer Stunden, die
nach der alten Regel weg gewesen waeren. Der neue Wert passt nicht mehr in
die bisherigen 16 Zeichen.

Revision ID: 0048_overtime_payout_forfeit
Revises: 0047_profil_vertragsstunden
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0048_overtime_payout_forfeit"
down_revision: str | None = "0047_profil_vertragsstunden"
branch_labels = None
depends_on = None

_KOMMENTAR = (
    "Ueberstunden-Auszahlung: none | all | above_cap | forfeit_above_cap "
    "(Ueberlauf ueber der Grenze verfaellt statt ausgezahlt zu werden)"
)


def upgrade() -> None:
    op.alter_column(
        "users",
        "overtime_payout_mode",
        existing_type=sa.String(length=16),
        type_=sa.String(length=24),
        existing_nullable=False,
        existing_server_default="above_cap",
        comment=_KOMMENTAR,
    )


def downgrade() -> None:
    # Bestehende Verfall-Einstellungen wuerden nicht mehr passen -> auf den
    # bisherigen Standard zuruecksetzen, sonst schlaegt das ALTER fehl.
    op.execute(
        "UPDATE users SET overtime_payout_mode = 'above_cap' "
        "WHERE overtime_payout_mode = 'forfeit_above_cap'"
    )
    op.alter_column(
        "users",
        "overtime_payout_mode",
        existing_type=sa.String(length=24),
        type_=sa.String(length=16),
        existing_nullable=False,
        existing_server_default="above_cap",
        comment="Überstunden-Auszahlung: none | all | above_cap",
    )
