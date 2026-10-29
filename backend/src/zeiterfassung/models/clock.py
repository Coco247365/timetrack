"""Clock models: punch_events, daily_timesheets, monthly_summaries."""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from zeiterfassung.models.base import Base, BigIntCompat, JSONBCompat, UUIDCompat


class EventType(enum.StrEnum):
    PUNCH_IN = "PUNCH_IN"
    PUNCH_OUT = "PUNCH_OUT"
    BREAK_START = "BREAK_START"
    BREAK_END = "BREAK_END"


class EventSource(enum.StrEnum):
    NFC_TERMINAL = "NFC_TERMINAL"
    PWA_MOBILE = "PWA_MOBILE"
    WEB_PORTAL = "WEB_PORTAL"
    ADMIN_MANUAL = "ADMIN_MANUAL"
    SYSTEM_AUTO = "SYSTEM_AUTO"
    # Automatischer Ausstempel, wenn eine Schicht länger als
    # settings.auto_clockout_hours (Default 14 h) offen war — vergessenes
    # Ausstempeln. Der Tag wird abgeleitet auf 0 h + Flag „Ausstempeln
    # vergessen" gesetzt, bis HR die echte Zeit einträgt (Korrektur ersetzt
    # dieses Event). Siehe clock_service.auto_clockout_overdue_shifts.
    SYSTEM_AUTO_TIMEOUT = "SYSTEM_AUTO_TIMEOUT"


class TimesheetStatus(enum.StrEnum):
    DRAFT = "DRAFT"
    COMPLETE = "COMPLETE"
    FLAGGED = "FLAGGED"
    APPROVED = "APPROVED"
    LOCKED = "LOCKED"


class PunchEvent(Base):
    """Immutable append-only record of a single clock event."""

    __tablename__ = "punch_events"

    id: Mapped[int] = mapped_column(BigIntCompat(), primary_key=True, autoincrement=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUIDCompat(),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        UUIDCompat(),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event_type: Mapped[EventType] = mapped_column(
        Enum(EventType, name="event_type_enum", native_enum=True), nullable=False
    )
    event_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    source: Mapped[EventSource] = mapped_column(
        Enum(EventSource, name="event_source_enum", native_enum=True), nullable=False
    )
    device_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Idempotenz-Schluessel vom Terminal (Offline-Queue): gleiche ID = gleicher Stempel.
    client_event_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # --- Korrektur-Unterstützung (append-only) ---
    # Der Kiosk schreibt Roh-Events; nichts wird je geändert oder gelöscht.
    # Eine manuelle Korrektur ist immer ein NEUES Event, das ein früheres
    # ersetzt (corrects_event_id) — entweder mit korrigierter Zeit oder als
    # Void (is_void=True, ersatzlos). Die Projektion (recompute_daily_timesheet)
    # ignoriert ersetzte und voidete Events. So bleibt das Log lückenlos.
    corrects_event_id: Mapped[int | None] = mapped_column(
        BigIntCompat(),
        ForeignKey("punch_events.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Wenn gesetzt: dieses Event ersetzt das genannte frühere Event.",
    )
    is_void: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        comment="True = das ersetzte Event wird ersatzlos entfernt (keine neue Zeit).",
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUIDCompat(),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        comment="Wer das manuelle/Korrektur-Event angelegt hat (None bei Kiosk-Events).",
    )
    created_by_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDCompat(),
        ForeignKey("accounts.id", ondelete="SET NULL"),
        nullable=True,
        comment=(
            "Login-Konto, das die Korrektur ausgefuehrt hat — auch Admin-Konten "
            "ohne Mitarbeiter-Verknuepfung (None bei Kiosk-Events und Altbestand)."
        ),
    )
    reason: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="Begründung der Korrektur (Pflicht im UI)."
    )

    def __repr__(self) -> str:
        return f"<PunchEvent id={self.id} type={self.event_type} time={self.event_time}>"


class DailyTimesheet(Base):
    """Computed daily work summary, derived from punch events."""

    __tablename__ = "daily_timesheets"
    __table_args__ = (
        UniqueConstraint("tenant_id", "employee_id", "work_date", name="uq_timesheet_per_day"),
    )

    id: Mapped[int] = mapped_column(BigIntCompat(), primary_key=True, autoincrement=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUIDCompat(),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        UUIDCompat(),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    work_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    start_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    break_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # HR-Korrektur des automatischen Pausen-Aufschlags (Pausenregel): Delta zu
    # den gestempelten Pausen in Minuten (>= 0). NULL = Automatik. Wird beim
    # Recompute NICHT ueberschrieben (wie weekend_booking).
    break_auto_override_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    net_work_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    gross_work_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    overtime_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[TimesheetStatus] = mapped_column(
        Enum(TimesheetStatus, name="timesheet_status_enum", native_enum=True),
        nullable=False,
        default=TimesheetStatus.DRAFT,
    )
    is_night_work: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_sunday_work: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_holiday_work: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Verbuchung von Wochenend-/Feiertagsarbeit: NULL = Überstunden (Default,
    # zählt nicht ins Konto, wird direkt vergütet), "gleitzeit" = ins Konto.
    weekend_booking: Mapped[str | None] = mapped_column(String(16), nullable=True)
    supplement_data: Mapped[dict | None] = mapped_column(
        JSONBCompat, nullable=True, comment="Zuschlagsdetails (night %, sunday %, holiday %)"
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    flagged_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    computed_from_events: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUIDCompat(), ForeignKey("users.id"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    def __repr__(self) -> str:
        return f"<DailyTimesheet employee={self.employee_id} date={self.work_date}>"


class MonthlySummary(Base):
    """Aggregated monthly statistics used for DATEV export."""

    __tablename__ = "monthly_summaries"
    __table_args__ = (
        UniqueConstraint("tenant_id", "employee_id", "year_month", name="uq_monthly_summary"),
    )

    id: Mapped[int] = mapped_column(BigIntCompat(), primary_key=True, autoincrement=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUIDCompat(),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        UUIDCompat(),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # First day of the month, e.g. 2025-01-01
    year_month: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    total_work_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_overtime_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    days_worked: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    days_sick: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    days_vacation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    days_holiday: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    night_supplement_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sunday_supplement_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    holiday_supplement_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    arbeitszeitkonto_balance_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    datev_exported_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    def __repr__(self) -> str:
        return f"<MonthlySummary employee={self.employee_id} month={self.year_month}>"
