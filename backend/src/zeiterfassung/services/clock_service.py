"""Clock service: punch recording, daily timesheet computation, anomaly detection."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

if TYPE_CHECKING:
    from zeiterfassung.api.deps import ScopeContext

from zeiterfassung.config import get_settings
from zeiterfassung.core.arbzg import validate_timesheet
from zeiterfassung.core.errors import DomainError, ErrorCode
from zeiterfassung.core.holidays import is_holiday, is_sunday
from zeiterfassung.db.scoping import get_scoped, tenant_scoped
from zeiterfassung.models.absence import ApprovalRequest, RequestStatus, RequestType
from zeiterfassung.models.clock import (
    DailyTimesheet,
    EventSource,
    EventType,
    PunchEvent,
    TimesheetStatus,
)
from zeiterfassung.models.device import Device
from zeiterfassung.models.tenant import Tenant
from zeiterfassung.models.user import User
from zeiterfassung.models.work_time_profile import WorkTimeProfile
from zeiterfassung.payroll.rules import is_midnight_continuation, parse_earliest_start
from zeiterfassung.policy import check as policy_check
from zeiterfassung.policy import statutory as policy_statutory
from zeiterfassung.policy import timekeeping as policy_timekeeping
from zeiterfassung.policy import worktime
from zeiterfassung.policy.context import AbsenceEntry, BreakInterval
from zeiterfassung.policy.overtime import (
    daily_target_minutes,
    hat_tagessoll,
    weekly_target_minutes,
)
from zeiterfassung.policy.tagesregeln import Tagesregeln
from zeiterfassung.services.tenant_service import (
    tenant_local_date,
    tenant_local_datetime,
    tenant_timezone,
)

_LIVING_ABSENCE_STATUSES = (
    RequestStatus.SUBMITTED,
    RequestStatus.APPROVED,
    RequestStatus.ESCALATED,
)

# Session GUC that the postgres append-only triggers on punch_events /
# approval_audit_log check (see alembic/versions/0036_append_only_triggers.py).
# Only ``undo_last_punch`` below is allowed to set this.
_APPEND_ONLY_GUC = "zeiterfassung.allow_append_only_mutation"


async def _assert_work_allowed_on_date(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    employee_id: uuid.UUID,
    work_date: date,
) -> None:
    """Blockt das Erfassen von Arbeitszeit, wenn der Tag als Krank markiert ist.

    Reine Daten-Beschaffung hier; die Entscheidung trifft
    ``policy.timekeeping.check_work_allowed_on_date``.
    """
    result = await db.execute(
        select(ApprovalRequest).where(
            ApprovalRequest.tenant_id == tenant_id,
            ApprovalRequest.employee_id == employee_id,
            ApprovalRequest.request_type == RequestType.SICK_LEAVE,
            ApprovalRequest.date_from <= work_date,
            ApprovalRequest.date_to >= work_date,
            ApprovalRequest.status.in_(_LIVING_ABSENCE_STATUSES),
        )
    )
    absences = [
        AbsenceEntry(
            date_from=r.date_from,
            date_to=r.date_to,
            day_part=r.day_part,
            request_type=str(r.request_type),
            status=str(r.status),
            request_id=str(r.id),
        )
        for r in result.scalars().all()
    ]
    for v in policy_check.check_work_entry(work_date, absences):
        if v.is_block:
            raise InvalidPunchStateError(v.message)


class NoBadgeUserError(DomainError):
    code = ErrorCode.NO_BADGE_USER


class AlreadyClockedInError(DomainError):
    code = ErrorCode.INVALID_PUNCH_STATE


class NotClockedInError(DomainError):
    code = ErrorCode.INVALID_PUNCH_STATE


class InvalidPunchStateError(DomainError):
    """Raised when a punch event violates the state machine."""

    code = ErrorCode.INVALID_PUNCH_STATE


class PunchRateLimitError(DomainError):
    """Raised when punches arrive faster than the minimum interval."""

    code = ErrorCode.PUNCH_RATE_LIMITED


class NothingToUndoError(DomainError):
    """Raised when there is no recent terminal punch to undo."""

    code = ErrorCode.NOTHING_TO_UNDO


class EventNotFoundError(DomainError):
    """Raised when a punch event referenced for correction does not exist."""

    code = ErrorCode.EVENT_NOT_FOUND


# Grace window for terminal undo: the last NFC-terminal event may be deleted
# within this many seconds of being created (accidental wrong-button recovery).
UNDO_GRACE_SECONDS = 90


# Minimum seconds between consecutive punch events for the same employee.
PUNCH_MIN_INTERVAL_SECONDS = 60

# Valid state transitions: maps last_event_type -> set of allowed next event types.
# None means "no previous event today" (initial state).
_VALID_TRANSITIONS: dict[EventType | None, set[EventType]] = {
    None: {EventType.PUNCH_IN},
    EventType.PUNCH_IN: {EventType.PUNCH_OUT, EventType.BREAK_START},
    EventType.PUNCH_OUT: {EventType.PUNCH_IN},
    EventType.BREAK_START: {EventType.BREAK_END},
    EventType.BREAK_END: {EventType.PUNCH_OUT, EventType.BREAK_START},
}


def _effective_events_stmt(tenant_id: uuid.UUID, employee_id: uuid.UUID):
    """Query auf die WIRKSAMEN Punch-Events eines Mitarbeiters.

    Wirksam = weder selbst ein Storno-Marker (``is_void``) noch durch eine
    spaetere Korrektur ersetzt (ein anderes Event zeigt per
    ``corrects_event_id`` darauf). Gleiche Definition wie in
    ``get_current_clock_status``/``recompute_daily_timesheet``/``punch_log`` —
    ohne sie sah die Zustandspruefung den Storno-Marker als aktuellen Stand und
    blockierte das Terminal nach jeder Korrektur (#185).
    """
    superseding = aliased(PunchEvent)
    return select(PunchEvent).where(
        PunchEvent.tenant_id == tenant_id,
        PunchEvent.employee_id == employee_id,
        PunchEvent.is_void == False,  # noqa: E712 — SQL-Vergleich, kein Python-bool
        ~select(superseding.id)
        .where(
            superseding.tenant_id == tenant_id,
            superseding.employee_id == employee_id,
            superseding.corrects_event_id == PunchEvent.id,
        )
        .exists(),
    )


async def _get_last_event(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    employee_id: uuid.UUID,
) -> PunchEvent | None:
    """Return the most recent EFFECTIVE punch event for this employee (any date).

    Stornierte und ersetzte Events zaehlen nicht — sonst widersprechen sich
    Vorschau (``/nfc/preview``, gefiltert) und Buchung (#185).
    """
    result = await db.execute(
        _effective_events_stmt(tenant_id, employee_id)
        .order_by(PunchEvent.event_time.desc(), PunchEvent.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


def _validate_state_transition(
    last_event: PunchEvent | None,
    new_type: EventType,
) -> None:
    """Validate that the requested event type is allowed given the current state."""
    last_type = last_event.event_type if last_event else None
    allowed = _VALID_TRANSITIONS.get(last_type, set())
    if new_type not in allowed:
        current_state = last_type.value if last_type else "no active session"
        if allowed:
            allowed_str = ", ".join(t.value for t in sorted(allowed, key=lambda x: x.value))
        else:
            allowed_str = "none"
        raise InvalidPunchStateError(
            f"Cannot record {new_type.value}: current state is '{current_state}'. "
            f"Allowed transitions: {allowed_str}"
        )


def _check_rate_limit(last_event: PunchEvent | None, now: datetime, new_type: EventType) -> None:
    """Reject duplicate punches that arrive too fast.

    Only rate-limits the SAME event type (prevents spam).
    Different actions in sequence are allowed (e.g. PUNCH_IN → BREAK_START).
    """
    if last_event is None:
        return
    # Only rate-limit same action type
    if last_event.event_type != new_type:
        return
    # Normalize both to UTC-aware for comparison (SQLite may return naive datetimes)
    last_time = last_event.event_time
    if last_time.tzinfo is None:
        last_time = last_time.replace(tzinfo=UTC)
    now_aware = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    elapsed = (now_aware - last_time).total_seconds()
    if elapsed < PUNCH_MIN_INTERVAL_SECONDS:
        remaining = int(PUNCH_MIN_INTERVAL_SECONDS - elapsed)
        raise PunchRateLimitError(
            f"Minimum interval between punches is {PUNCH_MIN_INTERVAL_SECONDS}s. "
            f"Please wait {remaining}s before the next punch."
        )


def _floor_to_minute(dt: datetime) -> datetime:
    """Stempelzeit auf die volle Minute abrunden — Sekunden zählen nie.

    11:17:34 → 11:17:00, 19:23:58 → 19:23:00. Gilt für alle Event-Quellen
    (Terminal, Web, manuelle Korrekturen), damit die Minutenrechnung der
    Tagesbögen nie von Sekundenresten abhängt."""
    return dt.replace(second=0, microsecond=0)


async def record_punch(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    employee_id: uuid.UUID,
    event_type: EventType,
    source: EventSource,
    device_id: str | None = None,
    ip_address: str | None = None,
    event_time: datetime | None = None,
    client_event_id: str | None = None,
) -> PunchEvent:
    """Append a punch event and trigger timesheet recomputation.

    Security:
    - event_time should only be provided by admin/system callers (NFC, admin correction).
      The /punch endpoint never passes it; the /nfc endpoint may pass it from the terminal.
    - State machine validation prevents invalid sequences (e.g. double PUNCH_IN).
    - Rate limiting prevents abuse (minimum 60s between events).
    """
    # Zeitrechnung ist minutengenau: Stempelzeit immer auf die volle Minute
    # abrunden (11:17:34 zählt als 11:17:00). created_at bleibt sekundengenau
    # (Undo-Grace, Audit).
    now = _floor_to_minute(event_time or datetime.now(tz=UTC))

    # Fetch last event for state validation and rate limiting
    last_event = await _get_last_event(db, tenant_id, employee_id)

    # Rate limiting: reject same action too fast (skipped for admin corrections w/ event_time)
    if event_time is None:
        _check_rate_limit(last_event, now, event_type)

    # State machine validation
    _validate_state_transition(last_event, event_type)

    event = PunchEvent(
        tenant_id=tenant_id,
        employee_id=employee_id,
        event_type=event_type,
        event_time=now,
        source=source,
        device_id=device_id,
        client_event_id=client_event_id,
        ip_address=ip_address,
        created_at=datetime.now(tz=UTC),
    )
    db.add(event)
    await db.flush()

    # Recompute the daily timesheet for the TENANT-LOCAL date of this event —
    # not the UTC date. A punch at 23:20 UTC is 00:20 CET for a Europe/Berlin
    # tenant and must land on the next calendar day (#3).
    tenant = await db.get(Tenant, tenant_id)
    work_date = tenant_local_date(now, tenant)
    await recompute_affected_days(db, tenant_id, employee_id, work_date)

    return event


# ---------------------------------------------------------------------------
# Manuelle Korrekturen (append-only): jede Korrektur ist ein neues Event.
# Kiosk-Roh-Events werden nie geändert oder gelöscht.
# ---------------------------------------------------------------------------


async def _auto_resettle_for_clock(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    employee_id: uuid.UUID,
    affected_date: date,
    actor_id: uuid.UUID | None,
) -> None:
    """Betroffenen, bereits abgeschlossenen Monat nach einer Stempel-Korrektur neu abschließen.

    Manuelle/korrigierte Events verschieben ggf. das Ist eines vergangenen,
    schon abgeschlossenen Monats — ``overtime_service.auto_resettle_if_needed``
    fängt alle Fehler selbst ab, damit der Stempel-Schreibpfad nie bricht.
    """
    from zeiterfassung.services import overtime_service

    await overtime_service.auto_resettle_if_needed(
        db, tenant_id, employee_id, affected_date, actor_id
    )


async def _insert_event(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    employee_id: uuid.UUID,
    event_type: EventType,
    event_time: datetime,
    *,
    source: EventSource = EventSource.ADMIN_MANUAL,
    created_by: uuid.UUID | None = None,
    created_by_account_id: uuid.UUID | None = None,
    reason: str | None = None,
    corrects_event_id: int | None = None,
    is_void: bool = False,
) -> PunchEvent:
    """Append a punch event WITHOUT state-machine/rate-limit checks.

    Used for admin corrections, which deliberately bypass the live-punch
    guards (you must be able to fix an arbitrary forgotten/wrong event).
    """
    event = PunchEvent(
        tenant_id=tenant_id,
        employee_id=employee_id,
        event_type=event_type,
        event_time=_floor_to_minute(event_time),
        source=source,
        created_at=datetime.now(tz=UTC),
        created_by=created_by,
        created_by_account_id=created_by_account_id,
        reason=reason,
        corrects_event_id=corrects_event_id,
        is_void=is_void,
    )
    db.add(event)
    await db.flush()
    return event


async def _load_event(db: AsyncSession, tenant_id: uuid.UUID, event_id: int) -> PunchEvent:
    result = await db.execute(
        select(PunchEvent).where(
            PunchEvent.tenant_id == tenant_id,
            PunchEvent.id == event_id,
        )
    )
    ev = result.scalar_one_or_none()
    if ev is None:
        raise EventNotFoundError(f"Stempel-Event {event_id} nicht gefunden.")
    return ev


async def _superseded_ids(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    employee_id: uuid.UUID,
    event_ids: list[int],
) -> set[int]:
    """Return the subset of *event_ids* that a later correction event replaces."""
    if not event_ids:
        return set()
    result = await db.execute(
        select(PunchEvent.corrects_event_id).where(
            PunchEvent.tenant_id == tenant_id,
            PunchEvent.employee_id == employee_id,
            PunchEvent.corrects_event_id.in_(event_ids),
        )
    )
    return {row[0] for row in result.all() if row[0] is not None}


async def add_manual_event(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    employee_id: uuid.UUID,
    event_type: EventType,
    event_time: datetime,
    created_by: uuid.UUID | None,
    reason: str | None = None,
    created_by_account_id: uuid.UUID | None = None,
) -> PunchEvent:
    """Add a single manual event (e.g. a forgotten clock-in/out) and recompute."""
    # Krank-Tag: keine Arbeitszeit erfassbar (Konflikt).
    await _assert_work_allowed_on_date(db, tenant_id, employee_id, event_time.date())
    ev = await _insert_event(
        db,
        tenant_id,
        employee_id,
        event_type,
        event_time,
        created_by=created_by,
        created_by_account_id=created_by_account_id,
        reason=reason,
    )
    # Plausibilität: Kommt vor Geht (verwirft die Eingabe bei Unsinn).
    await _assert_punch_order(db, tenant_id, employee_id, event_time.date())
    await recompute_affected_days(db, tenant_id, employee_id, event_time.date())
    await _assert_breaks_valid(db, tenant_id, employee_id, event_time.date())
    await _assert_work_time_remains(db, tenant_id, employee_id, event_time.date())
    await _auto_resettle_for_clock(db, tenant_id, employee_id, event_time.date(), created_by)
    return ev


async def _local_day_bounds(
    db: AsyncSession, tenant_id: uuid.UUID, work_date: date
) -> tuple[datetime, datetime]:
    """UTC-Grenzen des MANDANTEN-LOKALEN Kalendertags ``work_date`` (inklusiv).

    Tagesansicht, Reihenfolge-Pruefung und Pausenfenster muessen denselben Tag
    sehen wie der Tagesbogen (recompute_daily_timesheet) — sonst landet ein
    Stempel um 00:30 Lokalzeit (= 22:30 UTC am Vortag) in der Ansicht des
    Vortags und die Pruefung des eigentlichen Tags kennt ihn nicht
    ("Kommt muss vor Geht liegen" fuer ein korrektes Geht 06:15). Aufgefallen
    bei der Nachtschicht-Eingabe (Spec 2026-09-15).
    """
    tenant = await db.get(Tenant, tenant_id)
    tz = tenant_timezone(tenant)
    start_local = datetime(work_date.year, work_date.month, work_date.day, tzinfo=tz)
    end_local = start_local + timedelta(days=1) - timedelta(microseconds=1)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


async def _day_window_and_breaks(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    employee_id: uuid.UUID,
    work_date: date,
) -> tuple[datetime | None, datetime | None, list[BreakInterval]]:
    """Effektives Arbeitszeitfenster und bestehende Pausen eines Tages.

    Liefert (erstes Kommt, letztes Geht|None, Liste bestehender Pausen) — reine
    Daten-Beschaffung als Input für die Pausen-Policy. Superseded/void-Events
    werden ausgeblendet; ``work_end`` ist ``None``, wenn die Schicht noch offen ist.
    """
    day_start, day_end = await _local_day_bounds(db, tenant_id, work_date)
    result = await db.execute(
        select(PunchEvent)
        .where(
            PunchEvent.tenant_id == tenant_id,
            PunchEvent.employee_id == employee_id,
            PunchEvent.event_time >= day_start,
            PunchEvent.event_time <= day_end,
        )
        .order_by(PunchEvent.event_time, PunchEvent.id)
    )
    events = list(result.scalars().all())
    superseded = await _superseded_ids(db, tenant_id, employee_id, [e.id for e in events])
    effective = [e for e in events if e.id not in superseded and not e.is_void]

    ins = [e.event_time for e in effective if e.event_type == EventType.PUNCH_IN]
    outs = [e.event_time for e in effective if e.event_type == EventType.PUNCH_OUT]
    work_start = min(ins) if ins else None
    work_end = max(outs) if outs else None

    breaks: list[BreakInterval] = []
    open_start: datetime | None = None
    for e in effective:
        if e.event_type == EventType.BREAK_START:
            open_start = e.event_time
        elif e.event_type == EventType.BREAK_END and open_start is not None:
            breaks.append(BreakInterval(start=open_start, end=e.event_time))
            open_start = None
    return work_start, work_end, breaks


async def _assert_punch_order(
    db: AsyncSession, tenant_id: uuid.UUID, employee_id: uuid.UUID, work_date: date
) -> None:
    """Plausibilität der Stempelzeiten: jedes Geht muss nach seinem Kommt liegen.

    Fängt sinnlose manuelle Eingaben ab (z. B. Kommt 08:00, Geht 07:00). Betrachtet nur
    die effektiven (nicht ersetzten/gelöschten) Kommt/Geht-Events des Tages in Zeitfolge.
    """
    day_start, day_end = await _local_day_bounds(db, tenant_id, work_date)
    result = await db.execute(
        select(PunchEvent)
        .where(
            PunchEvent.tenant_id == tenant_id,
            PunchEvent.employee_id == employee_id,
            PunchEvent.event_time >= day_start,
            PunchEvent.event_time <= day_end,
        )
        .order_by(PunchEvent.event_time, PunchEvent.id)
    )
    events = list(result.scalars().all())
    superseded = await _superseded_ids(db, tenant_id, employee_id, [e.id for e in events])
    effective = [e for e in events if e.id not in superseded and not e.is_void]

    # Reine Reihenfolge-Regel liegt in policy/timekeeping.check_punch_order.
    punches = [
        (e.event_type == EventType.PUNCH_IN, e.event_time)
        for e in effective
        if e.event_type in (EventType.PUNCH_IN, EventType.PUNCH_OUT)
    ]
    for v in policy_timekeeping.check_punch_order(punches):
        if v.is_block:
            raise InvalidPunchStateError(v.message)


async def _assert_work_time_remains(
    db: AsyncSession, tenant_id: uuid.UUID, employee_id: uuid.UUID, work_date: date
) -> None:
    """Bei geschlossener Schicht muss Netto-Arbeitszeit > 0 bleiben.

    Fängt den Fall ab, dass Pausen die ganze Schicht auffressen (z. B. Kommt 08:00,
    Geht 09:00, Pause 08:00–09:00 → 0 h) — auch wenn die Pause auf noch offener Schicht
    eingetragen und das Geht erst danach ergänzt wurde.
    """
    ts = (
        await db.execute(
            select(DailyTimesheet).where(
                DailyTimesheet.tenant_id == tenant_id,
                DailyTimesheet.employee_id == employee_id,
                DailyTimesheet.work_date == work_date,
            )
        )
    ).scalar_one_or_none()
    if (
        ts is not None
        and ts.start_time is not None
        and ts.end_time is not None
        and int(ts.net_work_minutes or 0) <= 0
    ):
        raise InvalidPunchStateError(
            "Die erfassten Zeiten/Pausen lassen keine Arbeitszeit übrig — bitte prüfen."
        )


async def _assert_breaks_valid(
    db: AsyncSession, tenant_id: uuid.UUID, employee_id: uuid.UUID, work_date: date
) -> None:
    """Prüft ALLE Pausen des Tages gegen die Pausen-Policy (innerhalb Arbeitszeit, keine
    Überschneidung, lässt Arbeitszeit übrig).

    Nötig, weil das Bearbeiten einer Pause (Korrektur) sonst die Policy umgehen würde —
    ``add_break`` prüft nur beim Anlegen. Läuft nach jeder Korrektur/nachgetragenem Stempel.
    """
    work_start, work_end, breaks = await _day_window_and_breaks(
        db, tenant_id, employee_id, work_date
    )
    for i, b in enumerate(breaks):
        others = [x for j, x in enumerate(breaks) if j != i]
        for v in policy_check.check_new_break(
            b, work_start=work_start, work_end=work_end, existing=others
        ):
            if v.is_block:
                raise InvalidPunchStateError(v.message)


async def add_break(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    employee_id: uuid.UUID,
    start_time: datetime,
    end_time: datetime,
    created_by: uuid.UUID | None,
    reason: str | None = None,
    created_by_account_id: uuid.UUID | None = None,
) -> tuple[PunchEvent, PunchEvent]:
    """Add a break as a BREAK_START/BREAK_END pair (von–bis) and recompute.

    Multiple breaks per day are supported — each call appends another pair.
    Die Pausen-Regeln (Ende>Start, innerhalb Arbeitszeit, keine Überlappung)
    kommen aus der Policy-Schicht.
    """
    # Krank-Tag: keine Arbeitszeit (auch keine Pause) erfassbar.
    await _assert_work_allowed_on_date(db, tenant_id, employee_id, start_time.date())
    # ── Domäne B: Pausen-Policy (Kontext des Tages laden, Regeln entscheiden) ──
    work_start, work_end, existing_breaks = await _day_window_and_breaks(
        db, tenant_id, employee_id, start_time.date()
    )
    new_break = BreakInterval(start=start_time, end=end_time)
    for v in policy_check.check_new_break(
        new_break,
        work_start=work_start,
        work_end=work_end,
        existing=existing_breaks,
    ):
        if v.is_block:
            raise InvalidPunchStateError(v.message)

    start_ev = await _insert_event(
        db,
        tenant_id,
        employee_id,
        EventType.BREAK_START,
        start_time,
        created_by=created_by,
        created_by_account_id=created_by_account_id,
        reason=reason,
    )
    end_ev = await _insert_event(
        db,
        tenant_id,
        employee_id,
        EventType.BREAK_END,
        end_time,
        created_by=created_by,
        created_by_account_id=created_by_account_id,
        reason=reason,
    )
    await recompute_affected_days(db, tenant_id, employee_id, start_time.date())
    await _assert_work_time_remains(db, tenant_id, employee_id, start_time.date())
    return start_ev, end_ev


async def correct_event(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    event_id: int,
    new_time: datetime,
    created_by: uuid.UUID | None,
    reason: str | None = None,
    created_by_account_id: uuid.UUID | None = None,
) -> PunchEvent:
    """Replace an event's time by appending a superseding event. Original stays."""
    orig = await _load_event(db, tenant_id, event_id)
    new_ev = await _insert_event(
        db,
        tenant_id,
        orig.employee_id,
        orig.event_type,
        new_time,
        created_by=created_by,
        created_by_account_id=created_by_account_id,
        reason=reason,
        corrects_event_id=orig.id,
    )
    # Plausibilität nach der Korrektur: Kommt vor Geht.
    await _assert_punch_order(db, tenant_id, orig.employee_id, new_time.date())
    if new_time.date() != orig.event_time.date():
        await _assert_punch_order(db, tenant_id, orig.employee_id, orig.event_time.date())
    await recompute_affected_days(db, tenant_id, orig.employee_id, orig.event_time.date())
    if new_time.date() != orig.event_time.date():
        await recompute_affected_days(db, tenant_id, orig.employee_id, new_time.date())
    # Nach der Korrektur alle Tagesregeln erneut prüfen (sonst Policy-Bypass beim Editieren).
    for d in {new_time.date(), orig.event_time.date()}:
        await _assert_breaks_valid(db, tenant_id, orig.employee_id, d)
        await _assert_work_time_remains(db, tenant_id, orig.employee_id, d)
    await _auto_resettle_for_clock(db, tenant_id, orig.employee_id, new_time.date(), created_by)
    return new_ev


async def void_event(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    event_id: int,
    created_by: uuid.UUID | None,
    reason: str | None = None,
    created_by_account_id: uuid.UUID | None = None,
) -> PunchEvent:
    """Remove an event by appending a void marker that supersedes it. Original stays."""
    orig = await _load_event(db, tenant_id, event_id)
    void_ev = await _insert_event(
        db,
        tenant_id,
        orig.employee_id,
        orig.event_type,
        orig.event_time,
        created_by=created_by,
        created_by_account_id=created_by_account_id,
        reason=reason,
        corrects_event_id=orig.id,
        is_void=True,
    )
    await recompute_affected_days(db, tenant_id, orig.employee_id, orig.event_time.date())
    await _auto_resettle_for_clock(
        db, tenant_id, orig.employee_id, orig.event_time.date(), created_by
    )
    return void_ev


async def get_day_events(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    employee_id: uuid.UUID,
    work_date: date,
) -> list[dict[str, Any]]:
    """Return all punch events of a day with an ``effective`` flag.

    ``effective`` events are the current truth (they drive the calculation);
    superseded and voided rows are returned too (effective=False) so the UI can
    show the full correction history.
    """
    day_start, day_end = await _local_day_bounds(db, tenant_id, work_date)
    result = await db.execute(
        select(PunchEvent)
        .where(
            PunchEvent.tenant_id == tenant_id,
            PunchEvent.employee_id == employee_id,
            PunchEvent.event_time >= day_start,
            PunchEvent.event_time <= day_end,
        )
        .order_by(PunchEvent.event_time, PunchEvent.id)
    )
    events = list(result.scalars().all())
    superseded = await _superseded_ids(db, tenant_id, employee_id, [e.id for e in events])

    # Kappungs-Fenster fuer die Anzeige der GEWERTETEN Zeit (counted_time):
    # dieselbe Fallback-Kette wie recompute_daily_timesheet, damit die
    # Tagesansicht exakt das zeigt, was das Konto rechnet ("05:55 -> ab 06:00").
    tenant = await db.get(Tenant, tenant_id)
    employee_row = await get_scoped(db, User, tenant_id, employee_id)
    window = await _clock_window(db, tenant_id, tenant, employee_row)

    def _val(x: Any) -> str:
        return x.value if hasattr(x, "value") else str(x)

    # punch_events.device_id holds str(Device.id) for device-authenticated
    # (NFC terminal) punches; resolve it to a human-readable name + location so
    # the day-detail UI can show which terminal recorded the punch.
    device_ids: set[uuid.UUID] = set()
    for e in events:
        if e.device_id:
            try:
                device_ids.add(uuid.UUID(e.device_id))
            except ValueError:
                continue
    devices_by_id: dict[str, Device] = {}
    if device_ids:
        device_result = await db.execute(
            select(Device).where(Device.tenant_id == tenant_id, Device.id.in_(device_ids))
        )
        devices_by_id = {str(d.id): d for d in device_result.scalars().all()}

    def _device(e: PunchEvent) -> Device | None:
        return devices_by_id.get(e.device_id) if e.device_id else None

    # Gleiche Fenster wie der Tagesbogen (Nachtschicht-Fortsetzung ohne
    # Beginn-Grenze), damit die Anzeige zeigt, was gerechnet wird.
    wirksame = [e for e in events if e.id not in superseded and not e.is_void]
    window_by_id = dict(
        zip((e.id for e in wirksame), continuation_windows(wirksame, tenant, window), strict=True)
    )

    return [
        {
            "id": e.id,
            "event_type": _val(e.event_type),
            "event_time": e.event_time.isoformat(),
            # Gewertete Zeit (ISO-UTC): == event_time, wenn keine Kappung greift.
            "counted_time": counted_event_time(
                e.event_time, tenant, window_by_id.get(e.id, window)
            ).isoformat(),
            "source": _val(e.source),
            "reason": e.reason,
            "created_by": str(e.created_by) if e.created_by else None,
            "corrects_event_id": e.corrects_event_id,
            "is_void": e.is_void,
            "effective": e.id not in superseded and not e.is_void,
            "device_name": _device(e).name if _device(e) else None,
            "device_location": _device(e).location if _device(e) else None,
        }
        for e in events
    ]


async def get_punch_by_client_event_id(
    db: AsyncSession, tenant_id: uuid.UUID, client_event_id: str
) -> PunchEvent | None:
    """Look up a punch by its terminal-generated idempotency key (offline queue)."""
    result = await db.execute(
        select(PunchEvent).where(
            PunchEvent.tenant_id == tenant_id,
            PunchEvent.client_event_id == client_event_id,
        )
    )
    return result.scalar_one_or_none()


async def get_employee_by_badge(db: AsyncSession, tenant_id: uuid.UUID, badge_id: str) -> User:
    """Look up an employee by NFC badge ID."""
    result = await db.execute(
        select(User).where(
            User.tenant_id == tenant_id,
            User.nfc_badge_id == badge_id,
            User.is_active == True,  # noqa: E712
        )
    )
    user = result.scalar_one_or_none()
    if user is None:
        # Demo mode (env ZEITERFASSUNG_DEMO_AUTOASSIGN=true): the first time an
        # unknown card is tapped, bind its UID to the next badge-less active
        # employee so the terminal shows a real name instead of an error. Never
        # enabled in production.
        if get_settings().demo_autoassign:
            unassigned = await db.execute(
                select(User)
                .where(
                    User.tenant_id == tenant_id,
                    User.nfc_badge_id.is_(None),
                    User.is_active == True,  # noqa: E712
                    User.employee_number.like("DEMO%"),
                )
                .order_by(User.employee_number)
                .limit(1)
            )
            candidate = unassigned.scalar_one_or_none()
            if candidate is not None:
                candidate.nfc_badge_id = badge_id
                await db.commit()
                await db.refresh(candidate)
                return candidate
        raise NoBadgeUserError(f"No active employee found for badge {badge_id!r}")
    return user


async def undo_last_punch(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    employee_id: uuid.UUID,
) -> tuple[EventType, datetime]:
    """Delete the employee's most recent NFC-terminal punch if it was created
    within the grace window. Powers the terminal's "Rückgängig" button for
    accidental wrong-button recovery. Returns (event_type, event_time) of the
    deleted event. Raises NothingToUndoError if there's nothing eligible.

    Betrachtet nur WIRKSAME Events (kein Storno-Marker, nicht ersetzt) —
    gleiche Definition wie die Zustandspruefung (#185).
    """
    result = await db.execute(
        _effective_events_stmt(tenant_id, employee_id)
        .order_by(PunchEvent.event_time.desc(), PunchEvent.id.desc())
        .limit(1)
    )
    event = result.scalar_one_or_none()
    if event is None or event.source != EventSource.NFC_TERMINAL:
        raise NothingToUndoError("Kein Terminal-Eintrag zum Rückgängigmachen vorhanden.")
    # SQLite (tests) round-trips DateTime(timezone=True) as offset-naive;
    # Postgres (prod) returns it offset-aware. Normalize so the subtraction
    # below works identically on both.
    created_at = event.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    age = (datetime.now(UTC) - created_at).total_seconds()
    if age > UNDO_GRACE_SECONDS:
        raise NothingToUndoError("Zeitfenster zum Rückgängigmachen abgelaufen.")
    undone = (event.event_type, event.event_time)
    # punch_events is DB-enforced append-only (postgres trigger, migration
    # 0036_append_only_triggers) — this is the ONE sanctioned exception
    # (NFC-terminal mis-tap recovery within the grace window). The GUC is a
    # no-op on SQLite (dialect-guarded), so the test suite is unaffected.
    if db.get_bind().dialect.name == "postgresql":
        await db.execute(text(f"SET LOCAL {_APPEND_ONLY_GUC} = 'on'"))
    await db.delete(event)
    await db.flush()
    return undone


async def get_current_clock_status(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    employee_id: uuid.UUID,
) -> dict[str, Any]:
    """Return rich clock status: is_clocked_in, is_on_break, timestamps, today's minutes."""
    # Fetch all events, ordered most recent first
    result = await db.execute(
        select(PunchEvent)
        .where(
            PunchEvent.tenant_id == tenant_id,
            PunchEvent.employee_id == employee_id,
        )
        .order_by(PunchEvent.event_time.desc(), PunchEvent.id.desc())
    )
    all_events = list(result.scalars().all())

    # Nur effektive Events betrachten: ersetzte (durch eine Korrektur) und
    # voidete Events zählen nicht als aktueller Stempel-Stand. Sonst würde z. B.
    # ein gelöschtes "Geht" weiter als letztes Event gelten → fälschlich "raus".
    _superseded = {e.corrects_event_id for e in all_events if e.corrects_event_id is not None}
    all_events = [e for e in all_events if e.id not in _superseded and not e.is_void]

    if not all_events:
        return {
            "is_clocked_in": False,
            "is_on_break": False,
            "last_event": None,
            "clocked_in_since": None,
            "break_started_at": None,
        }

    last_event = all_events[0]

    # Anwesend ⇔ die aktuelle Sitzung ist OFFEN: es gibt ein Kommen ohne
    # späteres Gehen. Der Typ des letzten Events allein reicht NICHT — ein
    # verwaister Pause-Beginn NACH einem (z. B. nachgetragenen) Gehen darf
    # nicht als anwesend zählen (Tap am Folgetag auf offene Alt-Schicht,
    # deren Gehen der Admin später nachgetragen hat).
    # Rückwärts bis zum jüngsten Gehen laufen; jedes Kommen davor öffnet die
    # Sitzung, das älteste davon ist der Sitzungsbeginn.
    session_open = False
    clocked_in_since: datetime | None = None
    for ev in all_events:
        if ev.event_type == EventType.PUNCH_OUT:
            break
        if ev.event_type == EventType.PUNCH_IN:
            session_open = True
            clocked_in_since = ev.event_time

    is_clocked_in = session_open
    is_on_break = session_open and last_event.event_type == EventType.BREAK_START

    # Find break_started_at if currently on break
    break_started_at: datetime | None = None
    if is_on_break:
        break_started_at = last_event.event_time

    return {
        "is_clocked_in": is_clocked_in,
        "is_on_break": is_on_break,
        "last_event": last_event,
        "clocked_in_since": clocked_in_since,
        "break_started_at": break_started_at,
    }


async def get_presence_overview(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    scope: ScopeContext,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return presence status for all employees visible to the caller's scope.

    Uses a window function (ROW_NUMBER) to get the latest punch event per employee
    in a single query — portable across PostgreSQL and SQLite.
    """
    from sqlalchemy import func

    from zeiterfassung.models.department import Department

    # Scope guard: self-only and anonymized scopes cannot see individual presence
    if scope.is_self_only or scope.is_anonymized:
        return {
            "total_employees": 0,
            "present_count": 0,
            "on_break_count": 0,
            "employees": [],
        }

    # Tenant-lokales "heute" (wie ensure_auto_timesheets, #3) — und ein
    # injizierbares "jetzt" für zeitabhängige Tests (Auto-Anwesenheit).
    tenant = await db.get(Tenant, tenant_id)
    if now is None:
        now = datetime.now(tz=UTC)
    today = tenant_local_date(now, tenant)

    # Build user query with scope filtering
    users_stmt = (
        select(
            User.id.label("user_id"),
            User.first_name,
            User.last_name,
            User.department_id,
            User.time_capture,
            User.contracted_hours_per_week,
            User.weekday_target_minutes,
            User.work_time_profile_id,
            User.counting_start_date,
            User.hire_date,
            User.termination_date,
            Department.name.label("department_name"),
        )
        .outerjoin(Department, User.department_id == Department.id)
        .where(
            User.tenant_id == tenant_id,
            User.is_active == True,  # noqa: E712
        )
    )

    if scope.is_department:
        if not scope.dept_ids:
            return {
                "total_employees": 0,
                "present_count": 0,
                "on_break_count": 0,
                "employees": [],
            }
        users_stmt = users_stmt.where(User.department_id.in_(scope.dept_ids))

    users_result = await db.execute(users_stmt)
    users_rows = users_result.all()

    if not users_rows:
        return {
            "total_employees": 0,
            "present_count": 0,
            "on_break_count": 0,
            "employees": [],
        }

    user_ids = [r.user_id for r in users_rows]
    user_map = {
        r.user_id: {
            "first_name": r.first_name,
            "last_name": r.last_name,
            "department_id": r.department_id,
            "department_name": r.department_name,
        }
        for r in users_rows
    }
    auto_rows = [r for r in users_rows if (r.time_capture or "").lower() == "auto"]

    # Korrektur-Events ausblenden: ein per corrects_event_id ersetztes Event und
    # voidete Events sind nicht der aktuelle Stand. Ohne diesen Filter würde z. B.
    # ein gelöschtes "Geht" als letztes Event gelten → Mitarbeiter fälschlich "raus".
    superseded_subq = select(PunchEvent.corrects_event_id).where(
        PunchEvent.tenant_id == tenant_id,
        PunchEvent.corrects_event_id.isnot(None),
    )

    # Get the latest punch event per employee using a subquery with ROW_NUMBER
    row_num = (
        func.row_number()
        .over(partition_by=PunchEvent.employee_id, order_by=PunchEvent.event_time.desc())
        .label("rn")
    )
    latest_events_sub = (
        select(
            PunchEvent.employee_id,
            PunchEvent.event_type,
            PunchEvent.event_time,
            row_num,
        )
        .where(
            PunchEvent.tenant_id == tenant_id,
            PunchEvent.employee_id.in_(user_ids),
            PunchEvent.is_void.is_(False),
            PunchEvent.id.not_in(superseded_subq),
        )
        .subquery()
    )

    latest_result = await db.execute(
        select(
            latest_events_sub.c.employee_id,
            latest_events_sub.c.event_type,
            latest_events_sub.c.event_time,
        ).where(latest_events_sub.c.rn == 1)
    )
    latest_events = {row.employee_id: row for row in latest_result.all()}

    # Get today's timesheets for work minutes (+ Zeiten fürs Auto-Fenster)
    ts_result = await db.execute(
        select(
            DailyTimesheet.employee_id,
            DailyTimesheet.net_work_minutes,
            DailyTimesheet.start_time,
            DailyTimesheet.end_time,
        ).where(
            DailyTimesheet.tenant_id == tenant_id,
            DailyTimesheet.employee_id.in_(user_ids),
            DailyTimesheet.work_date == today,
        )
    )
    today_rows = {row.employee_id: row for row in ts_result.all()}
    today_minutes = {eid: row.net_work_minutes for eid, row in today_rows.items()}

    # Heutige erste Kommt- und letzte Geht-Zeit je Mitarbeiter (für "von–bis" in der Liste).

    day_start = datetime(today.year, today.month, today.day, tzinfo=UTC)
    day_end = day_start + timedelta(days=1)
    times_result = await db.execute(
        select(
            PunchEvent.employee_id,
            func.min(PunchEvent.event_time)
            .filter(PunchEvent.event_type == EventType.PUNCH_IN)
            .label("first_in"),
            func.max(PunchEvent.event_time)
            .filter(PunchEvent.event_type == EventType.PUNCH_OUT)
            .label("last_out"),
        )
        .where(
            PunchEvent.tenant_id == tenant_id,
            PunchEvent.employee_id.in_(user_ids),
            PunchEvent.is_void.is_(False),
            PunchEvent.id.not_in(superseded_subq),
            PunchEvent.event_time >= day_start,
            PunchEvent.event_time < day_end,
        )
        .group_by(PunchEvent.employee_id)
    )
    today_times = {row.employee_id: (row.first_in, row.last_out) for row in times_result.all()}

    # Kandidaten laut letztem Event-Typ. Ob die Sitzung wirklich OFFEN ist,
    # entscheidet danach der Vergleich jüngstes Kommen vs. jüngstes Gehen —
    # ein verwaister Pause-Beginn NACH einem (nachgetragenen) Gehen darf nicht
    # als anwesend zählen (gleiche Regel wie get_current_clock_status).
    candidate_ids = [
        eid
        for eid, ev in latest_events.items()
        if ev.event_type
        in (
            EventType.PUNCH_IN.value,
            EventType.BREAK_START.value,
            EventType.BREAK_END.value,
            EventType.PUNCH_IN,
            EventType.BREAK_START,
            EventType.BREAK_END,
        )
    ]

    clocked_in_since_map: dict[uuid.UUID, datetime] = {}
    if candidate_ids:
        # Jüngstes Kommen bzw. Gehen je Kandidat (Tie-Break über id wie die
        # Event-Sortierung: bei gleicher Minute gewinnt das später erfasste).
        def _latest_of(event_type: EventType) -> dict[uuid.UUID, tuple[datetime, int]]:
            rn = (
                func.row_number()
                .over(
                    partition_by=PunchEvent.employee_id,
                    order_by=(PunchEvent.event_time.desc(), PunchEvent.id.desc()),
                )
                .label("rn")
            )
            return (
                select(
                    PunchEvent.employee_id,
                    PunchEvent.event_time,
                    PunchEvent.id,
                    rn,
                )
                .where(
                    PunchEvent.tenant_id == tenant_id,
                    PunchEvent.employee_id.in_(candidate_ids),
                    PunchEvent.event_type == event_type,
                    PunchEvent.is_void.is_(False),
                    PunchEvent.id.not_in(superseded_subq),
                )
                .subquery()
            )

        pin_sub = _latest_of(EventType.PUNCH_IN)
        pin_result = await db.execute(
            select(pin_sub.c.employee_id, pin_sub.c.event_time, pin_sub.c.id).where(
                pin_sub.c.rn == 1
            )
        )
        last_in = {row.employee_id: (row.event_time, row.id) for row in pin_result.all()}

        pout_sub = _latest_of(EventType.PUNCH_OUT)
        pout_result = await db.execute(
            select(pout_sub.c.employee_id, pout_sub.c.event_time, pout_sub.c.id).where(
                pout_sub.c.rn == 1
            )
        )
        last_out = {row.employee_id: (row.event_time, row.id) for row in pout_result.all()}

        # Sitzung offen ⇔ jüngstes Kommen NACH dem jüngsten Gehen.
        for eid in candidate_ids:
            pin = last_in.get(eid)
            if pin is None:
                continue  # nie eingestempelt (nur verwaiste Pausen-Events)
            pout = last_out.get(eid)
            if pout is None or pin > pout:
                clocked_in_since_map[eid] = pin[0]

    # Nur Kandidaten mit wirklich offener Sitzung gelten als anwesend.
    clocked_in_ids = set(clocked_in_since_map)

    # Heutige genehmigte Abwesenheiten: ALLE Typen für den Auto-Anwesenheits-
    # Ausschluss (Urlaub/Krank/Sonderurlaub/unbezahlt/Überstundenabbau); als
    # Status angezeigt werden weiterhin nur Urlaub und Krank.
    from zeiterfassung.models.absence import ApprovalRequest, RequestStatus, RequestType

    absence_result = await db.execute(
        select(
            ApprovalRequest.employee_id,
            ApprovalRequest.request_type,
        ).where(
            ApprovalRequest.tenant_id == tenant_id,
            ApprovalRequest.employee_id.in_(user_ids),
            ApprovalRequest.status == RequestStatus.APPROVED,
            ApprovalRequest.date_from <= today,
            ApprovalRequest.date_to >= today,
            ApprovalRequest.request_type.in_(
                [
                    RequestType.VACATION,
                    RequestType.SICK_LEAVE,
                    RequestType.SPECIAL_LEAVE,
                    RequestType.UNPAID_LEAVE,
                    RequestType.OVERTIME_REDUCTION,
                ]
            ),
        )
    )
    # Map employee_id -> absence type (vacation or sick_leave)
    absence_map: dict[uuid.UUID, str] = {}
    absent_today: set[uuid.UUID] = set()
    for row in absence_result.all():
        rt = row.request_type if isinstance(row.request_type, str) else row.request_type.value
        absent_today.add(row.employee_id)
        if rt == "VACATION":
            absence_map[row.employee_id] = "vacation"
        elif rt == "SICK_LEAVE":
            absence_map[row.employee_id] = "sick_leave"

    # Auto-Erfasste gelten als anwesend, solange "jetzt" in den hinterlegten
    # Arbeitszeiten des heutigen Tages liegt — Tagesbogen (auch manuell
    # angepasst) gewinnt; ohne Bogen gilt das Fenster des aktiven 'auto'-
    # Arbeitszeit-Profils (Spec 2026-08-14: Wochentags-Zeiten, Tag ohne
    # Eintrag = frei → nicht anwesend; im Pausenfenster → on_break), sonst
    # das Standardfenster der Auto-Füllung (07:00 lokal + Tages-Soll +
    # Pflichtpause). Kein Fenster bei Abwesenheit, Wochenende, Feiertag,
    # "nicht gearbeitet"-Marker oder außerhalb des Beschäftigungszeitraums.
    auto_present: dict[uuid.UUID, datetime] = {}
    auto_on_break: set[uuid.UUID] = set()
    if auto_rows and today.weekday() < 5:
        from zeiterfassung.services.tenant_service import resolve_bundesland, tenant_timezone

        bundesland = await resolve_bundesland(db, tenant_id)
        if not is_holiday(today, bundesland):
            tz = tenant_timezone(tenant)
            from datetime import time as _time

            # Arbeitszeit-Profile einmal je Tenant laden (Muster
            # export_service._build_datev_rows), nicht je Mitarbeiter.
            wt_profiles: dict[uuid.UUID, WorkTimeProfile] = {}
            if any(r.work_time_profile_id for r in auto_rows):
                wt_profiles = {
                    p.id: p
                    for p in (
                        await db.execute(
                            select(WorkTimeProfile).where(WorkTimeProfile.tenant_id == tenant_id)
                        )
                    )
                    .scalars()
                    .all()
                }

            def _local(minute_of_day: int) -> datetime:
                return datetime.combine(
                    today, _time(minute_of_day // 60, minute_of_day % 60), tzinfo=tz
                )

            for r in auto_rows:
                uid = r.user_id
                if uid in clocked_in_ids or uid in absent_today:
                    continue
                start_cutoff = r.counting_start_date or r.hire_date
                if (start_cutoff and today < start_cutoff) or (
                    r.termination_date and today > r.termination_date
                ):
                    continue
                profile = (
                    wt_profiles.get(r.work_time_profile_id) if r.work_time_profile_id else None
                )
                aday = worktime.auto_day(profile, today.weekday())
                row = today_rows.get(uid)
                if row is not None:
                    # Bogen vorhanden: leerer Marker (net=0/keine Zeiten) heißt
                    # "nicht gearbeitet" — kein Fallback aufs Standardfenster.
                    if (row.net_work_minutes or 0) <= 0 or row.start_time is None:
                        continue
                    win_start, win_end = row.start_time, row.end_time
                elif worktime.auto_applies(profile):
                    # Profil gewinnt komplett: Tag ohne Eintrag = frei.
                    if aday is None:
                        continue
                    win_start, win_end = _local(aday.start_min), _local(aday.end_min)
                else:
                    soll = daily_target_minutes(
                        r.contracted_hours_per_week,
                        weekday=today.weekday(),
                        weekday_minutes=r.weekday_target_minutes,
                    )
                    if soll <= 0:
                        continue
                    brk = 45 if soll > 540 else 30 if soll > 360 else 0
                    win_start = datetime.combine(today, _time(7, 0), tzinfo=tz)
                    win_end = win_start + timedelta(minutes=soll + brk)
                if win_start is None or win_end is None:
                    continue
                if win_start <= now <= win_end:
                    auto_present[uid] = win_start
                    clocked_in_since_map.setdefault(uid, win_start)
                    # Liegt "jetzt" im festen Pausenfenster des Profils, ist
                    # der Status Pause statt anwesend — nur im Auto-Zweig,
                    # echte Stempler sind hier gar nicht (clocked_in_ids).
                    if (
                        aday is not None
                        and aday.break_start_min is not None
                        and aday.break_end_min is not None
                        and _local(aday.break_start_min) <= now < _local(aday.break_end_min)
                    ):
                        auto_on_break.add(uid)
                elif now > win_end:
                    # Fenster vorbei: die hinterlegte Arbeitszeit IST die heutige
                    # Anwesenheit ("von … bis …"). Ohne das stünde bei
                    # Auto-Erfassten ab Feierabend wieder „–", obwohl sie den
                    # ganzen Tag gearbeitet haben. Echte Stempelungen gewinnen
                    # (setdefault), der Status bleibt „abwesend" — sie sind weg.
                    today_times.setdefault(uid, (win_start, win_end))

    # Assemble results
    employees = []
    present_count = 0
    on_break_count = 0
    vacation_count = 0
    sick_leave_count = 0

    for uid in user_ids:
        info = user_map[uid]
        ev = latest_events.get(uid)

        # Anwesend nur bei wirklich offener Sitzung (jüngstes Kommen nach dem
        # jüngsten Gehen) — nicht allein nach dem Typ des letzten Events.
        is_clocked_in = uid in clocked_in_ids
        if ev is not None and is_clocked_in:
            et = ev.event_type
            is_on_break = et == ("BREAK_START" if isinstance(et, str) else EventType.BREAK_START)
        else:
            is_on_break = False

        absence_type = absence_map.get(uid)
        has_absence_conflict = False
        # Hat der/die MA heute überhaupt gestempelt? (Halbtags-Urlaub: gearbeitet + Urlaub.)
        stamped_today = today_times.get(uid, (None, None))[0] is not None

        if is_on_break:
            status = "on_break"
            on_break_count += 1
            present_count += 1
        elif is_clocked_in:
            status = "clocked_in"
            present_count += 1
        elif absence_type and not stamped_today:
            # Nur wenn NICHT gestempelt wurde, gewinnt die Abwesenheit. Wer heute
            # gearbeitet hat (z. B. Halbtags-Urlaub), zeigt seinen Anwesenheits-Status.
            status = absence_type
            if absence_type == "vacation":
                vacation_count += 1
            else:
                sick_leave_count += 1
        elif uid in auto_present:
            # Auto-Erfasste: "jetzt" liegt in den hinterlegten Arbeitszeiten;
            # im festen Pausenfenster des Auto-Profils gilt Pause statt anwesend.
            if uid in auto_on_break:
                status = "on_break"
                on_break_count += 1
            else:
                status = "clocked_in"
            present_count += 1
        else:
            # Gestempelt und wieder ausgestempelt (war da, jetzt weg).
            status = "clocked_out"

        employees.append(
            {
                "employee_id": uid,
                "first_name": info["first_name"],
                "last_name": info["last_name"],
                "department_id": info["department_id"],
                "department_name": info["department_name"],
                "status": status,
                "clocked_in_since": clocked_in_since_map.get(uid),
                "today_start": today_times.get(uid, (None, None))[0],
                "today_end": today_times.get(uid, (None, None))[1],
                "today_work_minutes": today_minutes.get(uid, 0),
                "has_absence_conflict": has_absence_conflict,
            }
        )

    # Build per-department summaries
    dept_stats: dict[str, dict[str, Any]] = {}
    for emp in employees:
        dept_name = emp["department_name"] or "Ohne Abteilung"
        dept_id = emp["department_id"]
        if dept_name not in dept_stats:
            dept_stats[dept_name] = {
                "department_id": dept_id,
                "department_name": dept_name,
                "total": 0,
                "present": 0,
                "on_break": 0,
                "vacation": 0,
                "sick_leave": 0,
                "absent": 0,
            }
        ds = dept_stats[dept_name]
        ds["total"] += 1
        if emp["status"] == "clocked_in":
            ds["present"] += 1
        elif emp["status"] == "on_break":
            ds["on_break"] += 1
        elif emp["status"] == "vacation":
            ds["vacation"] += 1
        elif emp["status"] == "sick_leave":
            ds["sick_leave"] += 1
        else:
            ds["absent"] += 1

    departments = sorted(dept_stats.values(), key=lambda d: d["department_name"])

    return {
        "total_employees": len(user_ids),
        "present_count": present_count,
        "on_break_count": on_break_count,
        "vacation_count": vacation_count,
        "sick_leave_count": sick_leave_count,
        "employees": employees,
        "departments": departments,
    }


async def backfill_auto_timesheets(db: AsyncSession, *, days: int = 60) -> int:
    """Fehlende Tagesboegen aller Auto-Erfasser nachziehen (Hintergrund-Job).

    ``ensure_auto_timesheets`` lief bisher nur beiläufig — beim Oeffnen der
    Mitarbeiter-Detailseite. Dadurch blieb ein Tag im Teamkalender weiss, bis
    jemand zufaellig hineinsah, und wurde danach gruen (Aktualisierungs-Bug).
    Dieser Job macht die Boegen unabhaengig von der Bedienung vollstaendig.
    Idempotent: legt nur fehlende Tage an. Gibt die Zahl der geprueften
    Mitarbeiter zurueck.
    """
    from datetime import timedelta

    rows = (
        await db.execute(
            select(User.id, User.tenant_id).where(
                User.is_active == True,  # noqa: E712
                User.time_capture == "auto",
            )
        )
    ).all()
    today = datetime.now(tz=UTC).date()
    for uid, tid in rows:
        await ensure_auto_timesheets(db, tid, uid, today - timedelta(days=days), today)
    return len(rows)


async def ensure_auto_timesheets(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    employee_id: uuid.UUID,
    date_from: date,
    date_to: date,
    *,
    today: date | None = None,
) -> None:
    """Auto-Erfassung: fehlende Werktage ab Erfassungsbeginn bis heute füllen.

    Für Mitarbeiter mit ``time_capture == "auto"`` wird an jedem Werktag das Tages-Soll
    (Wochenstunden ÷ 5, i. d. R. 8 h) als Tagesbogen eingetragen — rückwirkend ab dem
    Erfassungsbeginn (sonst Firmeneintritt) bis **heute** (nie in die Zukunft). Übersprungen
    werden Wochenenden, Feiertage, Abwesenheitstage und bereits vorhandene Tagesbögen.
    Idempotent: legt nur fehlende Tage an; bestehende (auch manuell korrigierte) bleiben.
    """
    from datetime import time

    from sqlalchemy.exc import IntegrityError

    user = await db.get(User, employee_id)
    if user is None or (user.time_capture or "").lower() != "auto":
        return
    tenant = await db.get(Tenant, tenant_id)
    if today is None:
        # Tenant-lokales "heute", nicht UTC — sonst würde kurz nach Mitternacht
        # Berliner Zeit noch der Vortag als Obergrenze gelten (#3).
        today = tenant_local_date(datetime.now(tz=UTC), tenant)
    start = user.counting_start_date or user.hire_date
    lo = max(date_from, start) if start else date_from
    hi = min(date_to, today)
    if lo > hi:
        return
    # Aktives 'auto'-Arbeitszeit-Profil (Spec 2026-08-14): gewinnt komplett —
    # die Wochentags-Zeiten des Profils ersetzen den 07:00-Default UND das
    # Soll aus Wochenstunden/Wochentagsplan. Einmal laden, gilt fuer alle Tage.
    wt_profile = await _work_time_profile(db, tenant_id, user)
    profile_applies = worktime.auto_applies(wt_profile) and hat_tagessoll(user.employment_type)
    # Soll wird je Tag bestimmt (wochentagsspezifischer Plan, sonst Wochenstunden÷5).
    weekday_minutes = user.weekday_target_minutes
    weekly_soll = weekly_target_minutes(user.contracted_hours_per_week, weekday_minutes)
    if not profile_applies and weekly_soll <= 0:
        return

    bundesland = (tenant.bundesland if tenant else None) or get_settings().default_bundesland
    tz = tenant_timezone(tenant)

    existing: dict[date, DailyTimesheet] = {
        r.work_date: r
        for r in (
            await db.execute(
                select(DailyTimesheet).where(
                    DailyTimesheet.tenant_id == tenant_id,
                    DailyTimesheet.employee_id == employee_id,
                    DailyTimesheet.work_date >= lo,
                    DailyTimesheet.work_date <= hi,
                )
            )
        )
        .scalars()
        .all()
    }

    # Genehmigte Abwesenheiten → diese Tage NICHT auto-füllen (Abwesenheit steht).
    absence_spans = (
        await db.execute(
            select(ApprovalRequest.date_from, ApprovalRequest.date_to).where(
                ApprovalRequest.tenant_id == tenant_id,
                ApprovalRequest.employee_id == employee_id,
                ApprovalRequest.status == RequestStatus.APPROVED,
                ApprovalRequest.request_type.in_(
                    [
                        RequestType.VACATION,
                        RequestType.SICK_LEAVE,
                        RequestType.SPECIAL_LEAVE,
                        RequestType.UNPAID_LEAVE,
                        RequestType.OVERTIME_REDUCTION,
                    ]
                ),
                ApprovalRequest.date_from <= hi,
                ApprovalRequest.date_to >= lo,
            )
        )
    ).all()
    absent: set[date] = set()
    for a_from, a_to in absence_spans:
        d = a_from
        while d <= a_to:
            absent.add(d)
            d += timedelta(days=1)

    now = datetime.now(tz=UTC)
    changed = False
    day = lo
    while day <= hi:
        if not (
            day.weekday() < 5  # Mo–Fr
            and not is_holiday(day, bundesland)
            and day not in absent
        ):
            day += timedelta(days=1)
            continue

        if profile_applies:
            # Profil gewinnt komplett: Zeiten/Pause des Wochentags aus dem
            # Profil (lokale Mandanten-Zeit); Tag ohne Eintrag = frei → KEIN
            # Tagesbogen. Pause = Pausenfenster, mind. gesetzliche Pflicht-
            # pause (AutoDay.break_minutes hebt selbst an); net = Fenster − Pause.
            aday = worktime.auto_day(wt_profile, day.weekday())
            if aday is None or aday.target_minutes <= 0:
                day += timedelta(days=1)
                continue
            soll_min = aday.target_minutes
            brk = aday.break_minutes
            start_dt = datetime.combine(
                day, time(aday.start_min // 60, aday.start_min % 60), tzinfo=tz
            )
            end_dt = datetime.combine(day, time(aday.end_min // 60, aday.end_min % 60), tzinfo=tz)
        else:
            # Tages-Soll dieses Wochentags; 0 = kein Arbeitstag laut Plan → überspringen.
            soll_min = daily_target_minutes(
                user.contracted_hours_per_week,
                weekday=day.weekday(),
                weekday_minutes=weekday_minutes,
                employment_type=user.employment_type,
            )
            if soll_min <= 0:
                day += timedelta(days=1)
                continue
            # Gesetzliche Mindestpause (ArbZG §4): >6 h → 30 min, >9 h → 45 min. Die Pause
            # liegt ZUSÄTZLICH zur Arbeitszeit (net = Soll bleibt, gross = net + Pause) —
            # kein ArbZG-Verstoß.
            brk = 45 if soll_min > 540 else 30 if soll_min > 360 else 0

            # Standard-Arbeitstag: 07:00 Start (Mandanten-Zeitzone), Pflichtpause,
            # Ende = Start + Soll + Pause.
            start_dt = datetime.combine(day, time(7, 0), tzinfo=tz)
            end_dt = start_dt + timedelta(minutes=soll_min + brk)

        row = existing.get(day)
        if row is None:
            db.add(
                DailyTimesheet(
                    tenant_id=tenant_id,
                    employee_id=employee_id,
                    work_date=day,
                    start_time=start_dt,
                    end_time=end_dt,
                    break_minutes=brk,
                    net_work_minutes=soll_min,
                    gross_work_minutes=soll_min + brk,
                    overtime_minutes=0,
                    status=TimesheetStatus.COMPLETE,
                    computed_from_events=False,
                    created_at=now,
                    updated_at=now,
                )
            )
            changed = True
        elif (
            row.computed_from_events is False
            and row.start_time is None
            and (row.net_work_minutes or 0) > 0
        ):
            # Alter Auto-Tagesbogen (net>0, noch ohne Zeiten/Pause) → kanonisch nachziehen.
            # Leere Marker (net=0 = „nicht gearbeitet") bleiben bewusst unberührt.
            row.start_time = start_dt
            row.end_time = end_dt
            row.break_minutes = brk
            row.net_work_minutes = soll_min
            row.gross_work_minutes = soll_min + brk
            row.status = TimesheetStatus.COMPLETE
            row.updated_at = now
            changed = True
        # sonst: echter/gestempelter/korrigierter Tag → unangetastet lassen.
        day += timedelta(days=1)

    if changed:
        try:
            await db.commit()
        except IntegrityError:
            # Anderer Request hat die Tage parallel angelegt — harmlos.
            await db.rollback()


async def set_manual_day(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    employee_id: uuid.UUID,
    work_date: date,
    start_hhmm: str,
    end_hhmm: str,
    break_minutes: int,
    actor_id: uuid.UUID | None = None,
) -> DailyTimesheet:
    """Tages-Arbeitszeit direkt am Tagesbogen setzen (Auto-Erfassung, ohne Stempel-Events).

    Kommt/Geht/Pause als reine Zeiten — daraus folgt net = (Geht − Kommt) − Pause,
    gross = (Geht − Kommt). Legt den Tagesbogen an oder aktualisiert den bestehenden;
    ``computed_from_events`` bleibt False (kein Event-basierter Tag).
    """
    from datetime import time
    from zoneinfo import ZoneInfo

    def _hhmm(s: str) -> tuple[int, int]:
        h, m = str(s).split(":")[:2]
        return int(h), int(m)

    tenant = await db.get(Tenant, tenant_id)
    try:
        tz = ZoneInfo(tenant.timezone if tenant and tenant.timezone else "Europe/Berlin")
    except Exception:
        tz = ZoneInfo("Europe/Berlin")

    # Regel: an Krankheitstagen keine Arbeitszeit (Entscheidung in der Policy).
    await _assert_work_allowed_on_date(db, tenant_id, employee_id, work_date)

    sh, sm = _hhmm(start_hhmm)
    eh, em = _hhmm(end_hhmm)
    start_dt = datetime.combine(work_date, time(sh, sm), tzinfo=tz)
    end_dt = datetime.combine(work_date, time(eh, em), tzinfo=tz)
    span = int((end_dt - start_dt).total_seconds() // 60)
    if span <= 0:
        raise ValueError("Geht muss nach Kommt liegen.")
    brk = max(0, int(break_minutes or 0))
    if brk >= span:
        raise ValueError("Die Pause darf nicht länger als die Anwesenheit sein.")
    net = span - brk

    now = datetime.now(tz=UTC)
    row = (
        await db.execute(
            select(DailyTimesheet).where(
                DailyTimesheet.tenant_id == tenant_id,
                DailyTimesheet.employee_id == employee_id,
                DailyTimesheet.work_date == work_date,
            )
        )
    ).scalar_one_or_none()
    if row is not None and row.status in (TimesheetStatus.APPROVED, TimesheetStatus.LOCKED):
        raise ValueError("Der Tag ist freigegeben/gesperrt und kann nicht geaendert werden.")
    alt_werte = _manual_day_audit_werte(row)
    if row is None:
        row = DailyTimesheet(
            tenant_id=tenant_id,
            employee_id=employee_id,
            work_date=work_date,
            overtime_minutes=0,
            created_at=now,
        )
        db.add(row)
    row.start_time = start_dt
    row.end_time = end_dt
    row.break_minutes = brk
    row.net_work_minutes = net
    row.gross_work_minutes = span
    row.status = TimesheetStatus.COMPLETE
    row.computed_from_events = False
    row.updated_at = now
    # Manuelle Tage haben keine Events hinter sich — jede Aenderung MUSS ins
    # Audit-Log (Append-only-Regel: in-place-Aenderung nur mit alt->neu-Spur).
    from zeiterfassung.services import audit_service

    await audit_service.log(
        db,
        tenant_id=tenant_id,
        actor_id=actor_id,
        action="timesheet.manual_day_set",
        resource_type="user",
        resource_id=str(employee_id),
        details={
            "work_date": work_date.isoformat(),
            "alt": alt_werte,
            "neu": _manual_day_audit_werte(row),
        },
    )
    await db.flush()
    return row


def _manual_day_audit_werte(row: DailyTimesheet | None) -> dict | None:
    """Kompakter alt/neu-Schnappschuss eines Tagesbogens fuers Audit-Log."""
    if row is None:
        return None
    return {
        "start_time": row.start_time.isoformat() if row.start_time else None,
        "end_time": row.end_time.isoformat() if row.end_time else None,
        "break_minutes": int(row.break_minutes or 0),
        "net_work_minutes": int(row.net_work_minutes or 0),
        "status": str(getattr(row.status, "value", row.status)),
        "computed_from_events": bool(row.computed_from_events),
    }


async def set_day_unworked(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    employee_id: uuid.UUID,
    work_date: date,
    actor_id: uuid.UUID | None = None,
) -> DailyTimesheet:
    """Tag als „nicht gearbeitet" markieren: leerer Tagesbogen (0 min, keine Zeit).

    Verhindert das automatische Auffüllen (der Tag existiert) und lässt den Tag als
    *unentschuldigtes Fehlen* zählen (−Tagessoll). Wird von Urlaub/Krank überschrieben
    (Abwesenheit deckt das Soll). Rückgängig über ``set_manual_day`` (Zeiten setzen).
    """
    now = datetime.now(tz=UTC)
    row = (
        await db.execute(
            select(DailyTimesheet).where(
                DailyTimesheet.tenant_id == tenant_id,
                DailyTimesheet.employee_id == employee_id,
                DailyTimesheet.work_date == work_date,
            )
        )
    ).scalar_one_or_none()
    if row is not None and row.status in (TimesheetStatus.APPROVED, TimesheetStatus.LOCKED):
        raise ValueError("Der Tag ist freigegeben/gesperrt und kann nicht geaendert werden.")
    alt_werte = _manual_day_audit_werte(row)
    if row is None:
        row = DailyTimesheet(
            tenant_id=tenant_id,
            employee_id=employee_id,
            work_date=work_date,
            overtime_minutes=0,
            created_at=now,
        )
        db.add(row)
    row.start_time = None
    row.end_time = None
    row.break_minutes = 0
    row.net_work_minutes = 0
    row.gross_work_minutes = 0
    row.status = TimesheetStatus.COMPLETE
    row.computed_from_events = False
    row.flagged_reason = None
    row.updated_at = now
    from zeiterfassung.services import audit_service

    await audit_service.log(
        db,
        tenant_id=tenant_id,
        actor_id=actor_id,
        action="timesheet.day_unworked_set",
        resource_type="user",
        resource_id=str(employee_id),
        details={"work_date": work_date.isoformat(), "alt": alt_werte},
    )
    await db.flush()
    return row


async def get_timesheets(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    employee_id: uuid.UUID,
    date_from: date,
    date_to: date,
    scope: ScopeContext | None = None,
) -> list[DailyTimesheet]:
    """Return daily timesheets filtered by scope.

    When *scope* is provided the *employee_id* argument is only used as the
    self-scope fallback (i.e. ``scope.user_id``).  Pass ``scope=None`` (the
    default) to preserve the original single-employee behaviour used by
    internal callers such as ``recompute_daily_timesheet``.
    """

    # Anonymized scope: BR sees no individual timesheets
    if scope is not None and scope.is_anonymized:
        return []

    stmt = select(DailyTimesheet).where(
        DailyTimesheet.tenant_id == tenant_id,
        DailyTimesheet.work_date >= date_from,
        DailyTimesheet.work_date <= date_to,
    )

    if scope is None or scope.is_self_only:
        # Legacy path and self-scope: filter by the given employee_id
        stmt = stmt.where(DailyTimesheet.employee_id == employee_id)
    elif scope.is_department:
        # Department scope: join users and filter by department_id
        if not scope.dept_ids:
            # No department assigned — fall back to own data only
            stmt = stmt.where(DailyTimesheet.employee_id == scope.user_id)
        else:
            stmt = stmt.join(User, DailyTimesheet.employee_id == User.id).where(
                User.department_id.in_(scope.dept_ids)
            )
    # is_global: no additional employee filter — all tenant data already scoped by tenant_id

    stmt = stmt.order_by(DailyTimesheet.work_date)
    result = await db.execute(stmt)
    return list(result.scalars().all())


# Flag-Text für einen automatisch (nach 14 h) beendeten, noch nicht von HR
# aufgelösten Tag. Wird in recompute_daily_timesheet gesetzt; im Frontend über
# die bestehende flagged_reason-Anzeige sichtbar.
FORGOTTEN_CLOCKOUT_FLAG = "Ausstempeln vergessen"


def _auto_clockout_reason(max_hours: int) -> str:
    return f"Automatisch ausgestempelt (Schicht > {max_hours} h offen)"


async def _current_shift_start(
    db: AsyncSession, tenant_id: uuid.UUID, employee_id: uuid.UUID
) -> datetime | None:
    """Beginn der aktuell offenen Schicht = letztes wirksames PUNCH_IN."""
    stmt = (
        _effective_events_stmt(tenant_id, employee_id)
        .where(PunchEvent.event_type == EventType.PUNCH_IN)
        .order_by(PunchEvent.event_time.desc(), PunchEvent.id.desc())
        .limit(1)
    )
    ev = (await db.execute(stmt)).scalars().first()
    return ev.event_time if ev is not None else None


async def auto_clockout_overdue_shifts(db: AsyncSession, *, now: datetime | None = None) -> int:
    """Kappt jede Schicht, die länger als ``settings.auto_clockout_hours`` offen ist.

    Für jede offene Schicht (letztes wirksames Event ≠ PUNCH_OUT), deren Beginn
    (letztes wirksames PUNCH_IN) mehr als N Stunden zurückliegt, wird bei
    ``Schichtbeginn + N h`` ein synthetisches PUNCH_OUT gesetzt (append-only,
    ``EventSource.SYSTEM_AUTO_TIMEOUT``). Eine offene Pause wird zuvor mit
    BREAK_END geschlossen. Der abgeleitete Tag wird dadurch 0 h + „Ausstempeln
    vergessen" (siehe ``recompute_daily_timesheet``), bis HR die echte Zeit
    einträgt (Korrektur ersetzt das Auto-Event).

    Normale Nachtschichten (< N h) bleiben unberührt. Idempotent — geschlossene
    Schichten werden nicht erneut angefasst. ``now`` ist injizierbar (Tests).
    Gibt die Zahl gekappter Schichten zurück.
    """

    max_hours = get_settings().auto_clockout_hours
    now = now or datetime.now(tz=UTC)
    cutoff = now - timedelta(hours=max_hours)

    pairs = (
        await db.execute(select(PunchEvent.tenant_id, PunchEvent.employee_id).distinct())
    ).all()

    closed = 0
    for tenant_id, employee_id in pairs:
        last = await _get_last_event(db, tenant_id, employee_id)
        if last is None or last.event_type == EventType.PUNCH_OUT:
            continue  # keine offene Schicht
        shift_start = await _current_shift_start(db, tenant_id, employee_id)
        if shift_start is None:
            continue
        start_aware = (
            shift_start if shift_start.tzinfo is not None else shift_start.replace(tzinfo=UTC)
        )
        if start_aware > cutoff:
            continue  # noch keine N h offen
        clock_out_at = start_aware + timedelta(hours=max_hours)
        reason = _auto_clockout_reason(max_hours)
        # Offene Pause zuerst schließen (Zustandsmaschine sauber → letzter Stand PUNCH_OUT).
        if last.event_type == EventType.BREAK_START:
            await _insert_event(
                db,
                tenant_id,
                employee_id,
                EventType.BREAK_END,
                clock_out_at,
                source=EventSource.SYSTEM_AUTO_TIMEOUT,
                reason=reason,
            )
        await _insert_event(
            db,
            tenant_id,
            employee_id,
            EventType.PUNCH_OUT,
            clock_out_at,
            source=EventSource.SYSTEM_AUTO_TIMEOUT,
            reason=reason,
        )
        tenant = await db.get(Tenant, tenant_id)
        # Das Auto-Ausstempeln schliesst eine Schicht, die ueber Mitternacht
        # gelaufen sein kann - ab dem Kommen neu rechnen, nicht erst ab dem Gehen.
        await recompute_affected_days(
            db, tenant_id, employee_id, tenant_local_date(start_aware, tenant)
        )
        closed += 1
    return closed


async def unresolved_auto_clockout_events(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    employee_ids: list[uuid.UUID],
    start_utc: datetime,
    end_utc: datetime,
) -> list[PunchEvent]:
    """Noch nicht aufgelöste Auto-Timeout-Ausstempel im UTC-Fenster [start, end).

    „Nicht aufgelöst" = das SYSTEM_AUTO_TIMEOUT-PUNCH_OUT ist weder voidet noch
    durch eine spätere Korrektur ersetzt. Basis für die Export-Sperre
    (Entscheidung 1: exportgesperrt bis HR auflöst).
    """
    if not employee_ids:
        return []
    superseding = aliased(PunchEvent)
    stmt = select(PunchEvent).where(
        PunchEvent.tenant_id == tenant_id,
        PunchEvent.employee_id.in_(employee_ids),
        PunchEvent.event_type == EventType.PUNCH_OUT,
        PunchEvent.source == EventSource.SYSTEM_AUTO_TIMEOUT,
        PunchEvent.is_void == False,  # noqa: E712
        PunchEvent.event_time >= start_utc,
        PunchEvent.event_time < end_utc,
        ~select(superseding.id)
        .where(
            superseding.tenant_id == tenant_id,
            superseding.corrects_event_id == PunchEvent.id,
        )
        .exists(),
    )
    return list((await db.execute(stmt)).scalars().all())


# Wie viele Folgetage eine offene Schicht hoechstens beeinflussen kann. Der
# Auto-Timeout schliesst nach 14 h; die Reserve faengt Faelle ab, in denen er
# (noch) nicht gelaufen ist, ohne bei einem Fehler ganze Monate neu zu rechnen.
_SPANNE_MAX_TAGE = 14


async def recompute_affected_days(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    employee_id: uuid.UUID,
    work_date: date,
) -> None:
    """Tagesbogen fuer ``work_date`` UND alle davon beeinflussten Folgetage neu rechnen.

    Ein Kommen ohne Gehen laeuft ueber Mitternacht weiter: der Tagesbogen des
    Folgetags haengt dann an einer Buchung, die gar nicht an diesem Tag liegt.
    Wird nur der Tag der Buchung neu gerechnet, behaelt der Folgetag seinen
    alten gespeicherten Wert — und der ist ueber die Oberflaeche nicht mehr
    korrigierbar, weil es dort keine Buchung zum Loeschen gibt (Kachel
    zeigt 8 h, Detailfenster 0 h).

    Neu gerechnet wird bis zu dem Tag, an dem die Schicht geschlossen ist —
    hoechstens bis heute und hoechstens ``_SPANNE_MAX_TAGE`` Tage weit.
    """
    tenant = await db.get(Tenant, tenant_id)
    heute = tenant_local_date(datetime.now(tz=UTC), tenant)
    ende = min(work_date + timedelta(days=_SPANNE_MAX_TAGE), max(work_date, heute))

    tag = work_date
    while tag <= ende:
        await recompute_daily_timesheet(db, tenant_id, employee_id, tag)
        if tag >= heute:
            break
        # Nur weiterlaufen, solange die Schicht am Tagesende noch offen ist.
        ts = await _stored_timesheet(db, tenant_id, employee_id, tag)
        if ts is None or not _schicht_offen(ts):
            break
        tag += timedelta(days=1)


async def _stored_timesheet(
    db: AsyncSession, tenant_id: uuid.UUID, employee_id: uuid.UUID, work_date: date
) -> DailyTimesheet | None:
    return (
        await db.execute(
            tenant_scoped(select(DailyTimesheet), DailyTimesheet, tenant_id).where(
                DailyTimesheet.employee_id == employee_id,
                DailyTimesheet.work_date == work_date,
            )
        )
    ).scalar_one_or_none()


def _schicht_offen(ts: DailyTimesheet) -> bool:
    """Tagesbogen mit Kommen, aber ohne Gehen — die Schicht laeuft weiter."""
    return ts.start_time is not None and ts.end_time is None


def _earliest_start_minutes(tenant: Tenant | None, user: User | None) -> int | None:
    """Profil-Regel ``fruehester_beginn`` des Mitarbeiters in Minuten seit Mitternacht.

    Ohne Profil oder ohne gesetzte Uhrzeit: ``None`` (keine Kappung). Gleiche Quelle
    wie Stundenzettel und DATEV-Export, damit alle drei dieselbe Zeit bewerten.
    """
    if tenant is None or user is None or not user.wage_profile_id:
        return None
    profile = next(
        (
            p
            for p in ((tenant.settings or {}).get("wage_profiles") or [])
            if str(p.get("id")) == str(user.wage_profile_id)
        ),
        None,
    )
    if not profile:
        return None
    return parse_earliest_start(profile.get("fruehester_beginn"))


async def _work_time_profile(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    user: User | None,
) -> WorkTimeProfile | None:
    """Zugewiesenes Arbeitszeit-Profil des Mitarbeiters laden (oder ``None``)."""
    if user is None or user.work_time_profile_id is None:
        return None
    return await get_scoped(db, WorkTimeProfile, tenant_id, user.work_time_profile_id)


def _window_from_profile(
    profile: WorkTimeProfile | None,
    tenant: Tenant | None,
    user: User | None,
) -> worktime.Window | None:
    """Kappungs-Fenster aus einem bereits geladenen Profil (Fallback-Kette).

    Aktives 'nfc'-Arbeitszeit-Profil (Beginn UND Ende, ggf. ueber Mitternacht)
    -> Lohnprofil-Regel ``fruehester_beginn`` (nur Beginn, wie bisher) ->
    ``None`` (keine Kappung). Ein inaktives oder 'auto'-Profil wirkt nicht und
    faellt auf die Lohnprofil-Regel zurueck.
    """
    window = worktime.nfc_window(profile)
    if window is not None:
        return window
    earliest_min = _earliest_start_minutes(tenant, user)
    if earliest_min is None:
        return None
    return worktime.Window(start_min=earliest_min, end_min=None)


async def _clock_window(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    tenant: Tenant | None,
    user: User | None,
) -> worktime.Window | None:
    """Kappungs-Fenster des Mitarbeiters — laedt das Profil und wertet es aus."""
    profile = await _work_time_profile(db, tenant_id, user)
    return _window_from_profile(profile, tenant, user)


def counted_event_time(
    dt: datetime, tenant: Tenant | None, window: worktime.Window | None
) -> datetime:
    """Gewertete Zeit eines Events (aware UTC): lokal ins Fenster geklemmt.

    Ohne Fenster (oder wenn der Stempel im Fenster liegt) kommt der Zeitpunkt
    unveraendert zurueck — das Roh-Event selbst wird nie angefasst. Fuer
    Fortsetzungssegmente einer Nachtschicht liefert ``continuation_windows``
    ein Fenster ohne Beginn-Grenze.
    """
    aware = dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    aware = aware.astimezone(UTC)
    if window is None:
        return aware
    local = tenant_local_datetime(aware, tenant)
    counted = worktime.counted_local(local, window)
    if counted == local:
        return aware
    return counted.astimezone(UTC)


def continuation_windows(
    events: list[PunchEvent], tenant: Tenant | None, window: worktime.Window | None
) -> list[worktime.Window | None]:
    """Je Event (in Zeitfolge) das wirksame Kappungsfenster.

    Ein Kommt um exakt 00:00 Lokalzeit eroeffnet die Fortsetzung einer
    Nachtschicht (Spec 2026-09-15). Fuer dieses Segment — bis einschliesslich
    seinem Geht — gilt die BEGINN-Grenze des Fensters nicht: weder der Beginn
    noch ein Geht/Pausenstempel vor der Schwelle (05:45 bei "fruehester
    Beginn 06:00") werden auf die Schwelle geschoben. Die ENDE-Grenze eines
    NFC-Fensters bleibt wirksam. Ausserhalb solcher Segmente gilt das Fenster
    unveraendert (Fruehstempler 05:19 -> 06:00, Fruehpausen kollabieren).
    """
    if window is None or window.start_min is None:
        return [window] * len(events)
    ohne_beginn = (
        worktime.Window(start_min=None, end_min=window.end_min)
        if window.end_min is not None
        else None
    )
    result: list[worktime.Window | None] = []
    in_continuation = False
    for e in events:
        if e.event_type == EventType.PUNCH_IN:
            in_continuation = is_midnight_continuation(tenant_local_datetime(e.event_time, tenant))
        result.append(ohne_beginn if in_continuation else window)
        if e.event_type == EventType.PUNCH_OUT:
            in_continuation = False
    return result


async def recompute_daily_timesheet(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    employee_id: uuid.UUID,
    work_date: date,
) -> DailyTimesheet:
    """Rebuild the DailyTimesheet row from raw punch events for the given date."""

    from zeiterfassung.services.tenant_service import resolve_bundesland

    bundesland = await resolve_bundesland(db, tenant_id)
    tenant = await db.get(Tenant, tenant_id)
    tz = tenant_timezone(tenant)

    # Fetch events for this date, ordered chronologically.
    # ``work_date`` is a TENANT-LOCAL calendar date; the query window must be
    # the local midnight-to-midnight span (converted to UTC for storage) — a
    # UTC calendar-day window mis-buckets events near local midnight (#3).
    # NOTE: this only aligns the day boundary to LOCAL midnight instead of UTC
    # midnight; it does not change how a shift that itself spans local
    # midnight is split/attributed across two DailyTimesheet rows — that's
    # tracked separately (#70/#96) and is deliberately out of scope here.
    day_start_local = datetime(work_date.year, work_date.month, work_date.day, tzinfo=tz)
    day_end_local = day_start_local + timedelta(days=1)
    day_start = day_start_local.astimezone(UTC)
    day_end = day_end_local.astimezone(UTC)

    result = await db.execute(
        select(PunchEvent)
        .where(
            PunchEvent.tenant_id == tenant_id,
            PunchEvent.employee_id == employee_id,
            PunchEvent.event_time >= day_start,
            PunchEvent.event_time < day_end,
        )
        .order_by(PunchEvent.event_time, PunchEvent.id)
    )
    day_events = list(result.scalars().all())

    # Korrektur-Filter: ersetzte und voidete Events zählen nicht. Eine Korrektur
    # ist ein neues Event, das ein früheres ersetzt (corrects_event_id); das
    # ersetzte Original und reine Void-Marker werden hier ausgeblendet. Das
    # Roh-Log bleibt unangetastet — nur die Projektion ignoriert sie.
    superseded = await _superseded_ids(db, tenant_id, employee_id, [e.id for e in day_events])
    day_events = [e for e in day_events if e.id not in superseded and not e.is_void]

    # Manuell gesetzte Tage (Auto-Erfassung / "Tag setzen" / "nicht gearbeitet",
    # computed_from_events=False) haben KEINE Events — der Rebuild wuerde sie
    # auf 0 h "nachrechnen". Ohne wirksame Events ist der gespeicherte manuelle
    # Bogen die Wahrheit und bleibt stehen (Tagesdetail zeigte 0h 00m,
    # waehrend Kalender/Zettel 4h aus dem Bogen lasen). Sobald echte Stempel
    # existieren, gewinnen die Events wie ueberall (Rebuild unten setzt dann
    # computed_from_events=True).
    if not day_events:
        stored = (
            await db.execute(
                select(DailyTimesheet).where(
                    DailyTimesheet.tenant_id == tenant_id,
                    DailyTimesheet.employee_id == employee_id,
                    DailyTimesheet.work_date == work_date,
                )
            )
        ).scalar_one_or_none()
        if stored is not None and stored.computed_from_events is False:
            return stored

    employee_row = await get_scoped(db, User, tenant_id, employee_id)

    # Helper: normalize to naive UTC for arithmetic
    def _naive_utc(dt: datetime) -> datetime:
        if dt.tzinfo is not None:
            return dt.astimezone(UTC).replace(tzinfo=None)
        return dt

    # Arbeitszeit-Profil EINMAL laden und kind-abhaengig nutzen (Spec
    # 2026-08-14): 'nfc' kappt die Stempel aufs erlaubte Fenster, 'auto'
    # ersetzt weiter unten das Tages-Soll. Fallback-Kette der Kappung:
    # aktives 'nfc'-Profil -> Lohnprofil-Regel "fruehester Beginn" (nur
    # Beginn) -> keine Kappung. Alle Events des Tages (auch BREAK_*) werden
    # fuer die Berechnung geklemmt; die Roh-Events bleiben unangetastet,
    # gekappt wird ausschliesslich diese Projektion.
    wt_profile = await _work_time_profile(db, tenant_id, employee_row)
    window = _window_from_profile(wt_profile, tenant, employee_row)

    def _counted(dt: datetime, w: worktime.Window | None = window) -> datetime:
        """Naive UTC-Zeit, in lokaler Mandanten-Zeit ins erlaubte Fenster geklemmt."""
        return _naive_utc(counted_event_time(dt, tenant, w))

    # Fortsetzungssegmente einer Nachtschicht (Kommt 00:00) ohne Beginn-Grenze.
    event_windows = continuation_windows(day_events, tenant, window)
    counted_by_id: dict[int, datetime] = {
        e.id: _counted(e.event_time, w) for e, w in zip(day_events, event_windows, strict=True)
    }

    # Compute gross, break, net minutes using a state machine.
    # We track the FIRST punch-in as start_time and the LAST punch-out as end_time.
    # Work segments: time between PUNCH_IN->BREAK_START, BREAK_END->PUNCH_OUT, etc.
    # This correctly handles interleaved sequences like:
    #   PUNCH_IN -> BREAK_START -> BREAK_END -> PUNCH_OUT
    #   PUNCH_IN -> BREAK_START -> BREAK_END -> BREAK_START -> BREAK_END -> PUNCH_OUT
    #   PUNCH_IN -> PUNCH_OUT -> PUNCH_IN -> PUNCH_OUT (multiple sessions per day)
    start_time: datetime | None = None  # first PUNCH_IN of the day
    end_time: datetime | None = None  # last PUNCH_OUT of the day
    start_counted: datetime | None = None  # gewertete Zeit des ersten Kommt
    end_counted: datetime | None = None  # gewertete Zeit des letzten Geht
    # Sekundengenau summieren und erst am ENDE auf Minuten runden — sonst
    # verliert jedes Segment fuer sich bis zu 59 Sekunden (drei 50-Sekunden-
    # Pausen wuerden als 0 Minuten Pause gezaehlt).
    break_seconds = 0.0
    work_seconds = 0.0
    gross_work_minutes = 0

    # State machine: track current segment start and whether we're in a work segment
    segment_start: datetime | None = None
    break_start: datetime | None = None

    for event in day_events:
        et = counted_by_id[event.id]
        if event.event_type == EventType.PUNCH_IN:
            if start_time is None:
                start_time = event.event_time  # preserve original tz-aware time
                start_counted = et
            segment_start = et
            break_start = None  # cancel any dangling break
        elif event.event_type == EventType.PUNCH_OUT:
            end_time = event.event_time  # preserve original tz-aware time
            end_counted = et
            if segment_start is not None:
                work_seconds += (et - segment_start).total_seconds()
                segment_start = None
            break_start = None  # cancel any dangling break
        elif event.event_type == EventType.BREAK_START:
            if segment_start is not None:
                work_seconds += (et - segment_start).total_seconds()
                segment_start = None
            break_start = et
        elif event.event_type == EventType.BREAK_END:
            if break_start is not None:
                break_seconds += (et - break_start).total_seconds()
                break_start = None
            segment_start = et  # resume work segment

    # Offene Schicht (kein PUNCH_OUT): nur am HEUTIGEN Tag live "bis jetzt" zählen.
    # Für vergangene Tage würde "bis jetzt" über Mitternacht hinauslaufen (bzw.
    # einen veralteten Live-Stand einfrieren) — dort zählt das offene Segment 0
    # und der Tag wird unten als "Gehen fehlt" markiert.
    now_naive = _naive_utc(datetime.now(tz=UTC))
    # work_date ist tenant-lokal -> "heute" ebenfalls tenant-lokal bestimmen
    # (sonst Fehlklassifikation zwischen 00:00 und 02:00 Lokalzeit, #3).
    is_today = work_date >= tenant_local_date(datetime.now(tz=UTC), tenant)
    # Offenes Segment am TAGESENDE: PUNCH_OUT/BREAK_* setzen segment_start/break_start
    # zurueck — steht hier noch etwas, laeuft die Schicht/Pause. Das gilt auch nach
    # einem frueheren Gehen am selben Tag (Kommt -> Geht -> wieder Kommt).
    open_no_out = segment_start is not None or break_start is not None
    if is_today:
        if segment_start is not None:
            work_seconds += (now_naive - segment_start).total_seconds()
        # If on an open break, count break up to now
        if break_start is not None:
            break_seconds += (now_naive - break_start).total_seconds()

    break_minutes = int(break_seconds / 60)
    work_minutes = int(work_seconds / 60)

    # Gross = Spanne erstes Kommen -> Ende:
    #   - Laufend heute (offene Schicht/Pause): bis jetzt
    #   - Vergangener Tag mit offener Schicht/Pause: bis zum letzten Event des Tages.
    #     WICHTIG: Auch wenn es zwischendurch ein Geht-Ereignis gab (zweite Schicht
    #     begonnen, aber nicht beendet), muss gross bis zum letzten Event reichen;
    #     end_time (letztes Geht) wuerde die zweite Schicht nicht abdecken und gross
    #     kleiner als net machen (Fehler: net > gross).
    #   - Abgeschlossener Tag (letztes Ereignis = Geht): bis zum letzten Geht.
    if start_time is not None:
        if open_no_out:
            if is_today:
                end_for_gross = now_naive
            else:
                end_for_gross = counted_by_id[day_events[-1].id] if day_events else start_counted
        elif end_time is not None:
            end_for_gross = end_counted
        else:
            end_for_gross = counted_by_id[day_events[-1].id] if day_events else start_counted
        gross_work_minutes = max(0, int((end_for_gross - start_counted).total_seconds() / 60))

    net_work_minutes = max(0, work_minutes)

    # Mindestpause automatisch anwenden: keine oder zu kurz gestempelte Pausen
    # werden im ABGELEITETEN Tagesbogen auf das Minimum angehoben; ausreichend
    # gestempelte Pausen bleiben exakt. Es gilt die betriebliche Regel (ab
    # Stichtag: > 4 h Arbeit → 45 min), davor die gesetzliche Staffel (ArbZG §4:
    # >6 h → 30 min, >9 h → 45 min) — siehe policy.statutory.
    # Basis ist die ANWESENHEIT = Arbeit + Pausen, NICHT die Tagesspanne: bei zwei
    # Schichten am Kalendertag (Kommen–Gehen–Kommen, z. B. Nachtschicht-Split) ist
    # die Lücke dazwischen weder Arbeit noch Pause und treibt die Pflicht nicht hoch.
    # Die Roh-Events bleiben unangetastet — nur break_minutes/net des Tagesbogens.
    presence_minutes = net_work_minutes + break_minutes
    punched_break_minutes = break_minutes  # gestempelte Pausen (vor der Anhebung)
    # HR-Korrektur des Aufschlags (Delta >= 0, NULL = Automatik) — bleibt am
    # Tagesbogen gespeichert und ueberlebt jeden Recompute.
    break_override = (
        await db.execute(
            select(DailyTimesheet.break_auto_override_minutes).where(
                DailyTimesheet.tenant_id == tenant_id,
                DailyTimesheet.employee_id == employee_id,
                DailyTimesheet.work_date == work_date,
            )
        )
    ).scalar_one_or_none()
    if break_override is not None:
        effective_break = punched_break_minutes + max(0, break_override)
    else:
        effective_break = policy_statutory.effective_break_minutes_betrieb(
            presence_minutes, break_minutes, work_date=work_date
        )
    # Dazugebucht wird ERST am Tagesende (letztes Gehen ist da): ein laufender
    # Tag zeigt nur die gestempelten Pausen — sonst schrumpft die Arbeitszeit
    # schon mittags um eine Pause, die noch kommt.
    day_closed = end_time is not None and not open_no_out
    if day_closed and effective_break > break_minutes:
        net_work_minutes = max(0, net_work_minutes - (effective_break - break_minutes))
        break_minutes = effective_break

    # Overtime: Tages-Soll dieses Wochentags. Ein aktives 'auto'-Arbeitszeit-
    # Profil gewinnt komplett (Fenster − Pause je Wochentag, freier Tag = 0),
    # ebenso ein aktives stundengebundenes NFC-Profil (Nachtrag 14.08.);
    # sonst wie bisher wochentagsspezifischer Plan bzw. Wochenstunden ÷ 5.
    employee = employee_row
    contracted_daily_minutes = 0
    if employee is not None:
        # Tages-Soll: EINE Stelle fuer alle Konsumenten (policy.tagesregeln).
        contracted_daily_minutes = Tagesregeln.fuer(employee, wt_profile).tagessoll(work_date)

    overtime_minutes = max(0, net_work_minutes - contracted_daily_minutes)

    # Work-type flags
    is_sunday_day = is_sunday(work_date)
    is_holiday_day = is_holiday(work_date, bundesland)

    # Tenant surcharge config for the night-window bounds (tenant already
    # loaded above for the local-date bucketing).
    z = (tenant.settings or {}).get("zuschlaege") or {} if tenant is not None else {}
    ns = int(z.get("night_start", 23))
    ne = int(z.get("night_end", 6))
    # Nachtarbeit auf den GEWERTETEN Zeiten pruefen: ein 05:56-Stempel, der durch
    # die Profil-Regel auf 06:00 rutscht, darf keinen Nachtzuschlag mehr ausloesen —
    # sonst zahlt die Abrechnung Nachtstunden fuer Zeit, die gar nicht zaehlt.
    is_night = any(
        _is_night_instant(counted_by_id[e.id].replace(tzinfo=UTC), ns, ne, tz) for e in day_events
    )

    # ArbZG validation — §4 mit echter Anwesenheit (nach evtl. Pausen-Anhebung),
    # §3 mit der ECHTEN Netto-Arbeitszeit (Summe der Arbeitssegmente): bei
    # geteilten Schichten sind Tagesspanne bzw. gross − breaks falsch, weil
    # die Lücke zwischen den Sessions weder Arbeit noch Anwesenheit ist.
    arbzg = validate_timesheet(
        gross_work_minutes,
        break_minutes if day_closed else max(break_minutes, effective_break),
        start_time,
        presence_minutes=net_work_minutes + break_minutes,
        net_work_minutes=net_work_minutes,
    )
    flagged = not arbzg.is_compliant or arbzg.has_warnings
    flagged_reason = "; ".join(v.message for v in arbzg.violations) if arbzg.violations else None

    # ArbZG-§4-Warnung auf GESTEMPELTER Basis: die automatische Anhebung heilt
    # nur das Zeitkonto, nicht den realen Gesetzesverstoß — wer die gesetzliche
    # Pause nicht wirklich genommen hat, soll das als Warnung sehen. Nur für
    # abgeschlossene Tage (laufende Schicht: die Pause kann noch kommen) und
    # erst ab dem Stichtag der Betriebsregel (Alt-Tage nicht nachträglich
    # umflaggen, wenn eine Korrektur sie neu berechnet).
    if work_date >= policy_statutory.BETRIEB_BREAK_GUELTIG_AB and day_closed:
        statutory_needed = policy_statutory.effective_break_minutes(
            presence_minutes, punched_break_minutes
        )
        if punched_break_minutes < statutory_needed:
            flagged = True
            warn = (
                f"ArbZG §4: Pause zu kurz gestempelt ({punched_break_minutes} min, "
                f"gesetzlich mind. {statutory_needed} min)"
            )
            flagged_reason = f"{flagged_reason}; {warn}" if flagged_reason else warn

    # Vergangener Tag mit Kommen ohne Gehen: sichtbar machen statt Zeit zu erfinden.
    if open_no_out and not is_today:
        flagged = True
        missing = "Gehen fehlt (offene Schicht)"
        flagged_reason = f"{flagged_reason}; {missing}" if flagged_reason else missing

    # Automatischer Timeout-Ausstempel (Spec 2026-08-03): schließt ein
    # SYSTEM_AUTO_TIMEOUT-PUNCH_OUT die Schicht und ist es noch nicht durch eine
    # HR-Korrektur ersetzt (steht also noch in den wirksamen day_events), gilt
    # der Tag als „Ausstempeln vergessen" → 0 h, geflaggt, NICHT saldowirksam
    # (net = 0). Das Auto-Event ist nur Schicht-Terminator, kein Stundenwert;
    # die echten Stunden trägt HR beim Auflösen ein (Korrektur ersetzt das Event).
    if any(
        e.event_type == EventType.PUNCH_OUT and e.source == EventSource.SYSTEM_AUTO_TIMEOUT
        for e in day_events
    ):
        net_work_minutes = 0
        overtime_minutes = 0
        flagged = True
        flagged_reason = FORGOTTEN_CLOCKOUT_FLAG

    now = datetime.now(tz=UTC)

    # Upsert: find existing or create new
    existing_result = await db.execute(
        select(DailyTimesheet).where(
            DailyTimesheet.tenant_id == tenant_id,
            DailyTimesheet.employee_id == employee_id,
            DailyTimesheet.work_date == work_date,
        )
    )
    ts = existing_result.scalar_one_or_none()

    if ts is None:
        ts = DailyTimesheet(
            tenant_id=tenant_id,
            employee_id=employee_id,
            work_date=work_date,
            created_at=now,
            updated_at=now,
        )
        db.add(ts)

    if ts.status not in (TimesheetStatus.APPROVED, TimesheetStatus.LOCKED):
        ts.start_time = start_time
        ts.end_time = end_time
        ts.break_minutes = break_minutes
        ts.net_work_minutes = net_work_minutes
        ts.gross_work_minutes = gross_work_minutes
        ts.overtime_minutes = overtime_minutes
        ts.is_sunday_work = is_sunday_day
        ts.is_holiday_work = is_holiday_day
        ts.is_night_work = is_night
        ts.computed_from_events = True
        ts.updated_at = now

        if flagged and ts.status not in (TimesheetStatus.APPROVED, TimesheetStatus.LOCKED):
            ts.status = TimesheetStatus.FLAGGED
            ts.flagged_reason = flagged_reason
        elif gross_work_minutes > 0 and end_time is not None:
            ts.status = TimesheetStatus.COMPLETE
            ts.flagged_reason = None
        else:
            ts.status = TimesheetStatus.DRAFT
            ts.flagged_reason = None  # alten Flag-Text nicht stehen lassen (z. B. nach Loeschen)

    await db.flush()
    return ts


def _is_night_instant(
    when: datetime,
    night_start: int,
    night_end: int,
    tz: ZoneInfo | None = None,
) -> bool:
    """True, wenn dieser Zeitpunkt im (ggf. ueber Mitternacht laufenden) Nachtfenster liegt."""
    hour = (when.astimezone(tz) if tz is not None else when).hour
    if night_start > night_end:
        return night_start <= hour or hour < night_end
    return night_start <= hour < night_end


def detect_anomalies(ts: DailyTimesheet | Any) -> list[str]:
    """Return a list of human-readable anomaly descriptions for a timesheet."""
    anomalies: list[str] = []

    if ts.gross_work_minutes > 600:  # > 10 h
        anomalies.append(f"Work time {ts.gross_work_minutes // 60}h exceeds 10-hour limit")

    # §4 bemisst sich an der Arbeitszeit (ohne Pausen), nicht an der Anwesenheitsspanne.
    net_for_breaks = ts.gross_work_minutes - ts.break_minutes
    if 360 < net_for_breaks <= 540 and ts.break_minutes < 30:
        anomalies.append("Work > 6h but break < 30 min (ArbZG §4)")

    if net_for_breaks > 540 and ts.break_minutes < 45:
        anomalies.append("Work > 9h but break < 45 min (ArbZG §4)")

    if ts.start_time is None and ts.end_time is not None:
        anomalies.append("Clock-out recorded without clock-in")

    if ts.start_time is not None and ts.end_time is None:
        anomalies.append("Clock-in recorded but no clock-out")

    return anomalies
