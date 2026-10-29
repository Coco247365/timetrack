"""employment_type auf 20 Zeichen verbreitern (neuer Wert 'werkstudent').

Werkstudenten/Aushilfen werden nur zeiterfasst: kein Tagessoll, nie im
DATEV-Export. Der Wert passt nicht mehr in die bisherigen 10 Zeichen.

Revision ID: 0045_employment_type_werkstudent
Revises: 0044_overtime_reduction_enum
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0045_employment_type_werkstudent"
down_revision: str | None = "0044_overtime_reduction_enum"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "users",
        "employment_type",
        existing_type=sa.String(length=10),
        type_=sa.String(length=20),
        existing_nullable=False,
        existing_server_default="hourly",
        comment=(
            "Beschaeftigungsart: salaried (Gehalt) | hourly (Stundenlohn) | "
            "werkstudent (nur Zeiterfassung: kein Tagessoll, nie im DATEV-Export)"
        ),
    )


def downgrade() -> None:
    # Vor dem Verengen die neuen Werte auf den Standard zuruecksetzen, sonst
    # schlaegt das ALTER an zu langen Bestandswerten fehl.
    op.execute("UPDATE users SET employment_type = 'hourly' WHERE length(employment_type) > 10")
    op.alter_column(
        "users",
        "employment_type",
        existing_type=sa.String(length=20),
        type_=sa.String(length=10),
        existing_nullable=False,
        existing_server_default="hourly",
        comment="Beschaeftigungsart: salaried (Gehalt) | hourly (Stundenlohn)",
    )
