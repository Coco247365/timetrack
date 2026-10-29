"""Arbeitszeit-Profile: Tabelle work_time_profiles + users.work_time_profile_id.

Spec 2026-08-14 (docs/superpowers/specs/2026-08-14-arbeitszeit-profile-design.md):
zwei Profil-Typen ('nfc' = Stempel-Fenster, 'auto' = Wochentags-Zeiten),
name unique je Mandant, Zuweisung am Mitarbeiter mit ON DELETE SET NULL.

Revision ID: 0046_work_time_profiles
Revises: 0045_employment_type_werkstudent
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from zeiterfassung.models.base import JSONBCompat, UUIDCompat

revision: str = "0046_work_time_profiles"
down_revision: str | None = "0045_employment_type_werkstudent"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "work_time_profiles",
        sa.Column("id", UUIDCompat(), primary_key=True),
        sa.Column(
            "tenant_id",
            UUIDCompat(),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("kind", sa.String(4), nullable=False, comment="nfc | auto"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "clock_start",
            sa.String(5),
            nullable=True,
            comment="HH:MM; NULL = keine Kappung am Beginn",
        ),
        sa.Column(
            "clock_end",
            sa.String(5),
            nullable=True,
            comment="HH:MM; NULL = keine Kappung am Ende",
        ),
        sa.Column("weekday_times", JSONBCompat(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("tenant_id", "name", name="uq_work_time_profiles_tenant_name"),
    )
    op.create_index("ix_work_time_profiles_tenant_id", "work_time_profiles", ["tenant_id"])

    op.add_column(
        "users",
        sa.Column(
            "work_time_profile_id",
            UUIDCompat(),
            sa.ForeignKey(
                "work_time_profiles.id",
                ondelete="SET NULL",
                name="fk_users_work_time_profile_id",
            ),
            nullable=True,
        ),
    )
    op.create_index("ix_users_work_time_profile_id", "users", ["work_time_profile_id"])


def downgrade() -> None:
    op.drop_index("ix_users_work_time_profile_id", table_name="users")
    op.drop_constraint("fk_users_work_time_profile_id", "users", type_="foreignkey")
    op.drop_column("users", "work_time_profile_id")
    op.drop_index("ix_work_time_profiles_tenant_id", table_name="work_time_profiles")
    op.drop_table("work_time_profiles")
