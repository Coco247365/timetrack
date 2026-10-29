"""Initial schema — all tables.

Revision ID: 0001
Revises:
Create Date: 2026-01-08
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # ENUM types are created implicitly by SQLAlchemy when the first table
    # using each type is rendered below (create_table emits CREATE TYPE and
    # de-duplicates across tables that share a type). We deliberately do NOT
    # pre-create them with raw DDL here: SQLAlchemy does not track raw
    # op.execute("CREATE TYPE …") statements, so a pre-create would collide
    # with SQLAlchemy's own emission ("type … already exists") on Postgres.
    # The matching downgrade() drops them explicitly with DROP TYPE IF EXISTS.
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # tenants
    # ------------------------------------------------------------------
    op.create_table(
        "tenants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="Europe/Berlin"),
        sa.Column("locale", sa.String(10), nullable=False, server_default="de-DE"),
        sa.Column("bundesland", sa.String(4), nullable=False, server_default="HH"),
        sa.Column("settings", postgresql.JSONB, nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    # ------------------------------------------------------------------
    # users
    # ------------------------------------------------------------------
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("hashed_password", sa.String(512), nullable=False),
        sa.Column("first_name", sa.String(100), nullable=False),
        sa.Column("last_name", sa.String(100), nullable=False),
        sa.Column("employee_number", sa.String(50), nullable=True),
        sa.Column("nfc_badge_id", sa.String(128), nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column(
            "contracted_hours_per_week", sa.Numeric(5, 2), nullable=False, server_default="40.0"
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_users_tenant_id", "users", ["tenant_id"])
    op.create_index("ix_users_email", "users", ["email"])
    op.create_index("ix_users_nfc_badge_id", "users", ["nfc_badge_id"])

    # ------------------------------------------------------------------
    # roles
    # ------------------------------------------------------------------
    op.create_table(
        "roles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("permissions", postgresql.JSONB, nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_roles_tenant_id", "roles", ["tenant_id"])

    # ------------------------------------------------------------------
    # user_roles
    # ------------------------------------------------------------------
    op.create_table(
        "user_roles",
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "role_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("roles.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )

    # ------------------------------------------------------------------
    # punch_events
    # ------------------------------------------------------------------
    op.create_table(
        "punch_events",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "employee_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "event_type",
            sa.Enum(
                "PUNCH_IN",
                "PUNCH_OUT",
                "BREAK_START",
                "BREAK_END",
                name="event_type_enum",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "source",
            sa.Enum(
                "NFC_TERMINAL",
                "PWA_MOBILE",
                "WEB_PORTAL",
                "ADMIN_MANUAL",
                "SYSTEM_AUTO",
                name="event_source_enum",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("device_id", sa.String(128), nullable=True),
        sa.Column("ip_address", sa.String(45), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_punch_events_tenant_id", "punch_events", ["tenant_id"])
    op.create_index("ix_punch_events_employee_id", "punch_events", ["employee_id"])
    op.create_index("ix_punch_events_event_time", "punch_events", ["event_time"])

    # ------------------------------------------------------------------
    # daily_timesheets
    # ------------------------------------------------------------------
    op.create_table(
        "daily_timesheets",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "employee_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("work_date", sa.Date, nullable=False),
        sa.Column("start_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("break_minutes", sa.Integer, nullable=False, server_default="0"),
        sa.Column("net_work_minutes", sa.Integer, nullable=False, server_default="0"),
        sa.Column("gross_work_minutes", sa.Integer, nullable=False, server_default="0"),
        sa.Column("overtime_minutes", sa.Integer, nullable=False, server_default="0"),
        sa.Column(
            "status",
            sa.Enum(
                "DRAFT",
                "COMPLETE",
                "FLAGGED",
                "APPROVED",
                "LOCKED",
                name="timesheet_status_enum",
                create_type=False,
            ),
            nullable=False,
            server_default="DRAFT",
        ),
        sa.Column("is_night_work", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("is_sunday_work", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("is_holiday_work", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("supplement_data", postgresql.JSONB, nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("flagged_reason", sa.Text, nullable=True),
        sa.Column("computed_from_events", sa.Boolean, nullable=False, server_default="true"),
        sa.Column(
            "approved_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True
        ),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("tenant_id", "employee_id", "work_date", name="uq_timesheet_per_day"),
    )
    op.create_index("ix_daily_timesheets_tenant_id", "daily_timesheets", ["tenant_id"])
    op.create_index("ix_daily_timesheets_employee_id", "daily_timesheets", ["employee_id"])
    op.create_index("ix_daily_timesheets_work_date", "daily_timesheets", ["work_date"])

    # ------------------------------------------------------------------
    # timesheet_corrections
    # ------------------------------------------------------------------
    op.create_table(
        "timesheet_corrections",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "timesheet_id",
            sa.BigInteger,
            sa.ForeignKey("daily_timesheets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "correction_type",
            sa.Enum(
                "MANUAL_TIME",
                "BREAK_ADJUSTMENT",
                "STATUS_CHANGE",
                "NOTE_UPDATE",
                "SUPPLEMENT_CORRECTION",
                name="correction_type_enum",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("field_changed", sa.Text, nullable=True),
        sa.Column("old_value", sa.Text, nullable=True),
        sa.Column("new_value", sa.Text, nullable=True),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column(
            "requested_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column(
            "approved_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_timesheet_corrections_tenant_id", "timesheet_corrections", ["tenant_id"])
    op.create_index(
        "ix_timesheet_corrections_timesheet_id", "timesheet_corrections", ["timesheet_id"]
    )

    # ------------------------------------------------------------------
    # monthly_summaries
    # ------------------------------------------------------------------
    op.create_table(
        "monthly_summaries",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "employee_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("year_month", sa.Date, nullable=False),
        sa.Column("total_work_minutes", sa.Integer, nullable=False, server_default="0"),
        sa.Column("total_overtime_minutes", sa.Integer, nullable=False, server_default="0"),
        sa.Column("days_worked", sa.Integer, nullable=False, server_default="0"),
        sa.Column("days_sick", sa.Integer, nullable=False, server_default="0"),
        sa.Column("days_vacation", sa.Integer, nullable=False, server_default="0"),
        sa.Column("days_holiday", sa.Integer, nullable=False, server_default="0"),
        sa.Column("night_supplement_minutes", sa.Integer, nullable=False, server_default="0"),
        sa.Column("sunday_supplement_minutes", sa.Integer, nullable=False, server_default="0"),
        sa.Column("holiday_supplement_minutes", sa.Integer, nullable=False, server_default="0"),
        sa.Column(
            "arbeitszeitkonto_balance_minutes", sa.Integer, nullable=False, server_default="0"
        ),
        sa.Column("datev_exported_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("tenant_id", "employee_id", "year_month", name="uq_monthly_summary"),
    )
    op.create_index("ix_monthly_summaries_tenant_id", "monthly_summaries", ["tenant_id"])
    op.create_index("ix_monthly_summaries_employee_id", "monthly_summaries", ["employee_id"])
    op.create_index("ix_monthly_summaries_year_month", "monthly_summaries", ["year_month"])

    # ------------------------------------------------------------------
    # approval_requests
    # ------------------------------------------------------------------
    op.create_table(
        "approval_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "employee_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "request_type",
            sa.Enum(
                "VACATION",
                "SICK_LEAVE",
                "UNPAID_LEAVE",
                "SPECIAL_LEAVE",
                "CORRECTION",
                name="request_type_enum",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "DRAFT",
                "SUBMITTED",
                "APPROVED",
                "REJECTED",
                "ESCALATED",
                "CANCELLED",
                name="request_status_enum",
                create_type=False,
            ),
            nullable=False,
            server_default="DRAFT",
        ),
        sa.Column("date_from", sa.Date, nullable=False),
        sa.Column("date_to", sa.Date, nullable=False),
        sa.Column("days_count", sa.Numeric(6, 2), nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "decided_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_reason", sa.Text, nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_approval_requests_tenant_id", "approval_requests", ["tenant_id"])
    op.create_index("ix_approval_requests_employee_id", "approval_requests", ["employee_id"])

    # ------------------------------------------------------------------
    # approval_audit_log
    # ------------------------------------------------------------------
    op.create_table(
        "approval_audit_log",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "request_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("approval_requests.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "old_status",
            sa.Enum(
                "DRAFT",
                "SUBMITTED",
                "APPROVED",
                "REJECTED",
                "ESCALATED",
                "CANCELLED",
                name="request_status_enum",
                create_type=False,
            ),
            nullable=True,
        ),
        sa.Column(
            "new_status",
            sa.Enum(
                "DRAFT",
                "SUBMITTED",
                "APPROVED",
                "REJECTED",
                "ESCALATED",
                "CANCELLED",
                name="request_status_enum",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "actor_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True
        ),
        sa.Column(
            "actor_type",
            sa.Enum("USER", "SYSTEM", name="actor_type_enum", create_type=False),
            nullable=False,
        ),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_approval_audit_log_request_id", "approval_audit_log", ["request_id"])

    # ------------------------------------------------------------------
    # payroll_exports
    # ------------------------------------------------------------------
    op.create_table(
        "payroll_exports",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("year_month", sa.Date, nullable=False),
        sa.Column(
            "format",
            sa.Enum("datev_csv", name="export_format_enum", create_type=False),
            nullable=False,
            server_default="datev_csv",
        ),
        sa.Column(
            "status",
            sa.Enum(
                "PREVIEW",
                "GENERATED",
                "DELIVERED",
                "FAILED",
                name="export_status_enum",
                create_type=False,
            ),
            nullable=False,
            server_default="PREVIEW",
        ),
        sa.Column("file_checksum", sa.String(64), nullable=True),
        sa.Column("row_count", sa.Integer, nullable=True),
        sa.Column(
            "generated_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True
        ),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_payroll_exports_tenant_id", "payroll_exports", ["tenant_id"])

    # ------------------------------------------------------------------
    # wage_type_mappings
    # ------------------------------------------------------------------
    op.create_table(
        "wage_type_mappings",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("internal_code", sa.String(64), nullable=False),
        sa.Column("datev_lohnart", sa.Integer, nullable=False),
        sa.Column("description", sa.String(255), nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_wage_type_mappings_tenant_id", "wage_type_mappings", ["tenant_id"])

    # ------------------------------------------------------------------
    # audit_log
    # ------------------------------------------------------------------
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "actor_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True
        ),
        sa.Column("action", sa.String(128), nullable=False),
        sa.Column("resource_type", sa.String(64), nullable=True),
        sa.Column("resource_id", sa.String(128), nullable=True),
        sa.Column("ip_address", sa.String(45), nullable=True),
        sa.Column("details", postgresql.JSONB, nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_audit_log_tenant_id", "audit_log", ["tenant_id"])
    op.create_index("ix_audit_log_action", "audit_log", ["action"])
    op.create_index("ix_audit_log_created_at", "audit_log", ["created_at"])


def downgrade() -> None:
    op.drop_table("audit_log")
    op.drop_table("wage_type_mappings")
    op.drop_table("payroll_exports")
    op.drop_table("approval_audit_log")
    op.drop_table("approval_requests")
    op.drop_table("monthly_summaries")
    op.drop_table("timesheet_corrections")
    op.drop_table("daily_timesheets")
    op.drop_table("punch_events")
    op.drop_table("user_roles")
    op.drop_table("roles")
    op.drop_table("users")
    op.drop_table("tenants")

    op.execute("DROP TYPE IF EXISTS export_status_enum")
    op.execute("DROP TYPE IF EXISTS export_format_enum")
    op.execute("DROP TYPE IF EXISTS correction_type_enum")
    op.execute("DROP TYPE IF EXISTS actor_type_enum")
    op.execute("DROP TYPE IF EXISTS request_status_enum")
    op.execute("DROP TYPE IF EXISTS request_type_enum")
    op.execute("DROP TYPE IF EXISTS timesheet_status_enum")
    op.execute("DROP TYPE IF EXISTS event_source_enum")
    op.execute("DROP TYPE IF EXISTS event_type_enum")
