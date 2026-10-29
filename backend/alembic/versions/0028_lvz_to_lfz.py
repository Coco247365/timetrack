"""LVZ → LFZ: gespeicherte Krankmeldungs-Status und Lohnart-Kategorien umbenennen.

Fachlich korrekt ist LFZ (Lohnfortzahlung); LVZ war ein Tippfehler in Enum-Werten
und Kategorie-Codes. Betrifft:
- approval_requests.sick_status (VARCHAR-Enum): AU_MIT_LVZ → AU_MIT_LFZ usw.
- tenants.settings["lohnart_zuordnung"]: Kategorie-Schlüssel KRANK_OHNE_AU_*_LVZ → _LFZ.
- wage_type_mappings.internal_code: vorsorglich (falls Kategorien dort gelandet sind).

Revision ID: 0028_lvz_to_lfz
Revises: 0027_user_weekday_target
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0028_lvz_to_lfz"
down_revision: str | None = "0027_user_weekday_target"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "UPDATE approval_requests SET sick_status = REPLACE(sick_status, 'LVZ', 'LFZ') "
        "WHERE sick_status LIKE '%LVZ%'"
    )
    op.execute(
        "UPDATE wage_type_mappings SET internal_code = REPLACE(internal_code, 'LVZ', 'LFZ') "
        "WHERE internal_code LIKE '%LVZ%'"
    )
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # JSONB-Schlüssel in tenants.settings.lohnart_zuordnung umbenennen.
        op.execute(
            """
            UPDATE tenants
            SET settings = jsonb_set(
                settings,
                '{lohnart_zuordnung}',
                (
                    SELECT jsonb_object_agg(REPLACE(key, 'LVZ', 'LFZ'), value)
                    FROM jsonb_each(settings->'lohnart_zuordnung')
                )
            )
            WHERE settings ? 'lohnart_zuordnung'
              AND settings->>'lohnart_zuordnung' LIKE '%LVZ%'
            """
        )


def downgrade() -> None:
    op.execute(
        "UPDATE approval_requests SET sick_status = REPLACE(sick_status, 'LFZ', 'LVZ') "
        "WHERE sick_status LIKE '%LFZ%'"
    )
    op.execute(
        "UPDATE wage_type_mappings SET internal_code = REPLACE(internal_code, 'LFZ', 'LVZ') "
        "WHERE internal_code LIKE '%LFZ%'"
    )
