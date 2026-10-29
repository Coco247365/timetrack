"""Clock / timesheet schemas."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated

from pydantic import BaseModel, Field, field_validator, model_validator

from zeiterfassung.models.clock import EventSource, EventType, TimesheetStatus


class PunchRequest(BaseModel):
    event_type: EventType
    source: EventSource = EventSource.WEB_PORTAL
    device_id: str | None = None


class NFCPunchRequest(BaseModel):
    """Used by NFC terminals — authenticated by device API key."""

    # Bounded like RegisterToPoolRequest.token_ids (max_length=64) — a real
    # DESFire token-id is well within this; the bound is a hardening guard
    # against oversized payloads on the device-authenticated NFC surface,
    # not a functional constraint.
    badge_id: Annotated[str, Field(min_length=1, max_length=64)]
    event_type: EventType = EventType.PUNCH_IN
    # Deprecated/ignored: the recorded device is always the AUTHENTICATED device
    # from the X-Device-Api-Key (see api/v1/clock.py DeviceCtx / device.device_id).
    # A client-supplied device_id must never be trusted for the punch record —
    # kept only for backward compat with older terminal payloads.
    device_id: str | None = None
    # Offline-Queue: Original-Zeitstempel + Idempotenz-ID vom Terminal (optional).
    event_time: datetime | None = None
    client_event_id: str | None = None


class NFCLeaveRequest(BaseModel):
    """Vacation request submitted from an NFC terminal (device-authenticated).

    The terminal only picks a date range; the request type is always VACATION
    (a kiosk must not be able to file e.g. sick leave), days_count is computed
    server-side, and the request lands as SUBMITTED for manager approval.
    """

    badge_id: str
    date_from: date
    date_to: date
    notes: str | None = None


class RegisterToPoolRequest(BaseModel):
    """Batch of freshly personalized DESFire token-ids reported by the on-terminal
    provisioning script (device-authenticated — no separate admin token). Each
    token is pooled into the tenant's scanned-cards pool so it shows up in the
    personnel board immediately, without a per-fob terminal tap.
    """

    token_ids: list[Annotated[str, Field(min_length=4, max_length=64)]] = Field(
        min_length=1,
        max_length=500,
        description="DESFire token-ids to add to the scan pool (SAM-authenticated by the caller).",
    )
    uids: (
        list[Annotated[str, Field(min_length=8, max_length=20, pattern=r"^[0-9a-fA-F]+$")] | None]
        | None
    ) = Field(
        default=None,
        description=(
            "Optional physical chip UIDs (non-secret, hex — 7 bytes/14 hex chars is typical "
            "but validated tolerantly as 8-20 hex chars), parallel-indexed to token_ids. "
            "A given entry may be null if the UID for that token is not (yet) known. If "
            "provided at all, must be the same length as token_ids. Backward compatible: "
            "omit entirely for callers that only report token_ids."
        ),
    )

    @field_validator("uids")
    @classmethod
    def _normalize_uids(cls, v: list[str | None] | None) -> list[str | None] | None:
        if v is None:
            return v
        return [u.upper() if u is not None else None for u in v]

    @model_validator(mode="after")
    def _check_uids_length(self) -> RegisterToPoolRequest:
        if self.uids is not None and len(self.uids) != len(self.token_ids):
            raise ValueError("uids, if provided, must have the same length as token_ids")
        return self


class RegisterToPoolResponse(BaseModel):
    """Result of a register-to-pool batch call."""

    pooled: int
    skipped_existing: int
    skipped_assigned: int
    pool_size: int
    backfilled: int = 0


class RegisterAdminCardsRequest(BaseModel):
    """Batch of freshly personalized DESFire token-ids to flag as admin fobs.

    Admin fobs gate the wall-terminal's personalize mode (tap admin fob -> the
    kiosk unlocks batch personalization of blank fobs). Device-authenticated,
    same conventions as ``RegisterToPoolRequest``. Admin fobs are intentionally
    NOT added to the scanned-cards pool (``/nfc/register-to-pool``) — they must
    never be assignable as an employee badge. Revocation (removing a token from
    ``tenant.settings["admin_cards"]``) is a future web-admin feature; this
    endpoint only ever adds.
    """

    token_ids: list[Annotated[str, Field(min_length=4, max_length=64)]] = Field(
        min_length=1,
        max_length=50,
        description="DESFire token-ids to flag as admin fobs (SAM-authenticated by the caller).",
    )


class RegisterAdminCardsResponse(BaseModel):
    """Result of a register-admin-cards batch call."""

    added: int
    already_admin: int
    admin_count: int


class PunchEventResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    tenant_id: uuid.UUID
    employee_id: uuid.UUID
    event_type: EventType
    event_time: datetime
    source: EventSource
    device_id: str | None
    created_at: datetime


class ManualEventRequest(BaseModel):
    """Add a single forgotten/manual event (e.g. clock-in or clock-out)."""

    employee_id: uuid.UUID
    event_type: EventType
    event_time: datetime
    reason: str | None = None


class ManualBreakRequest(BaseModel):
    """Add a break as a von–bis pair. Multiple breaks per day are allowed."""

    employee_id: uuid.UUID
    start_time: datetime
    end_time: datetime
    reason: str | None = None


class CorrectEventRequest(BaseModel):
    """Replace an event's time (appends a superseding correction event)."""

    event_time: datetime
    reason: str | None = None


class VoidEventRequest(BaseModel):
    """Remove an event (appends a void marker that supersedes it)."""

    reason: str | None = None


class DayEventEntry(BaseModel):
    """One row of a day's punch log, with an ``effective`` flag for the UI."""

    id: int
    event_type: str
    event_time: datetime
    # Gewertete Zeit (Stempel-Fenster des Arbeitszeit-Profils bzw. Lohnprofil-
    # Regel "fruehester Beginn"): == event_time, wenn keine Kappung greift.
    counted_time: datetime | None = None
    source: str
    reason: str | None = None
    created_by: uuid.UUID | None = None
    corrects_event_id: int | None = None
    is_void: bool = False
    effective: bool = True
    device_name: str | None = None
    device_location: str | None = None


class DailyTimesheetResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    tenant_id: uuid.UUID
    employee_id: uuid.UUID
    work_date: date
    start_time: datetime | None
    end_time: datetime | None
    break_minutes: int
    net_work_minutes: int
    gross_work_minutes: int
    overtime_minutes: int
    status: TimesheetStatus
    is_night_work: bool
    is_sunday_work: bool
    is_holiday_work: bool
    # Wochenend-/Feiertagsarbeit: None = Überstunden (Direktvergütung), "gleitzeit" = Konto.
    weekend_booking: str | None = None
    # Pausen-Aufschlag: NULL = Automatik (Pausenregel), sonst HR-Wert (Delta).
    break_auto_override_minutes: int | None = None
    supplement_data: dict | None
    notes: str | None
    flagged_reason: str | None
    computed_from_events: bool
    approved_by: uuid.UUID | None
    approved_at: datetime | None
    locked_at: datetime | None
    created_at: datetime
    updated_at: datetime


class DayDetailResponse(BaseModel):
    """Full picture of one day: effective+historic events plus the timesheet."""

    work_date: date
    events: list[DayEventEntry]
    timesheet: DailyTimesheetResponse | None = None


class ClockStatusResponse(BaseModel):
    is_clocked_in: bool
    is_on_break: bool = False
    last_event: PunchEventResponse | None
    clocked_in_since: datetime | None = None
    break_started_at: datetime | None = None
    today_work_minutes: int
    today_break_minutes: int = 0
    today_overtime_minutes: int = 0


class EmployeePresence(BaseModel):
    employee_id: uuid.UUID
    first_name: str
    last_name: str
    department_id: uuid.UUID | None = None
    department_name: str | None = None
    status: str  # "clocked_in" | "on_break" | "clocked_out" | "vacation" | "sick_leave"
    clocked_in_since: datetime | None = None
    today_start: datetime | None = None  # erste Kommt-Zeit heute
    today_end: datetime | None = None  # letzte Geht-Zeit heute
    today_work_minutes: int = 0
    has_absence_conflict: bool = False  # True if clocked in during approved absence


class DepartmentPresenceSummary(BaseModel):
    department_id: uuid.UUID | None = None
    department_name: str
    total: int
    present: int = 0
    on_break: int = 0
    vacation: int = 0
    sick_leave: int = 0
    absent: int = 0


class PresenceOverviewResponse(BaseModel):
    total_employees: int
    present_count: int
    on_break_count: int
    vacation_count: int = 0
    sick_leave_count: int = 0
    employees: list[EmployeePresence]
    departments: list[DepartmentPresenceSummary] = []
