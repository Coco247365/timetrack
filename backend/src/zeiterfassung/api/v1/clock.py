"""Clock in/out API endpoints."""

from __future__ import annotations

import time
import uuid
from collections import defaultdict, deque
from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from zeiterfassung.api.deps import (
    AdminUser,
    AuthUser,
    CurrentUser,
    DBSession,
    DeviceContext,
    DeviceCtx,
)
from zeiterfassung.config import get_settings
from zeiterfassung.core.errors import ErrorCode, api_error
from zeiterfassung.core.holidays import is_holiday
from zeiterfassung.db.scoping import tenant_scoped
from zeiterfassung.models.absence import RequestType
from zeiterfassung.models.clock import (
    DailyTimesheet,
    EventSource,
    EventType,
    PunchEvent,
)
from zeiterfassung.models.device import Device
from zeiterfassung.models.tenant import Tenant
from zeiterfassung.models.user import User
from zeiterfassung.schemas.absence import AbsenceRequestCreate
from zeiterfassung.schemas.clock import (
    ClockStatusResponse,
    CorrectEventRequest,
    DailyTimesheetResponse,
    DayDetailResponse,
    DayEventEntry,
    DepartmentPresenceSummary,
    EmployeePresence,
    ManualBreakRequest,
    ManualEventRequest,
    NFCLeaveRequest,
    NFCPunchRequest,
    PresenceOverviewResponse,
    PunchEventResponse,
    PunchRequest,
    RegisterAdminCardsRequest,
    RegisterAdminCardsResponse,
    RegisterToPoolRequest,
    RegisterToPoolResponse,
    VoidEventRequest,
)
from zeiterfassung.services import absence_service, clock_service
from zeiterfassung.services.tenant_service import tenant_local_date

router = APIRouter()

_NFC_ACTION_LABELS = {
    EventType.PUNCH_IN: "clock_in",
    EventType.PUNCH_OUT: "clock_out",
    EventType.BREAK_START: "break_start",
    EventType.BREAK_END: "break_end",
}


def _device_event_time(candidate: datetime | None) -> datetime | None:
    """Validate an original timestamp sent by a terminal (offline queue).

    Only device-authenticated endpoints call this. Outside the plausibility
    window (max. 7 days back, 5 min future drift) the timestamp is discarded
    and server time applies.
    """
    if candidate is None:
        return None
    if candidate.tzinfo is None:
        candidate = candidate.replace(tzinfo=UTC)
    server_now = datetime.now(tz=UTC)
    if server_now - timedelta(days=7) <= candidate <= server_now + timedelta(minutes=5):
        return candidate
    return None


def _resolve_device_tenant_id(request: Request, device: DeviceContext) -> uuid.UUID:
    """Resolve the tenant for a device-authenticated NFC request.

    The tenant is authoritative from the device's DB registration (resolved by
    ``get_device`` from the ``X-Device-Api-Key``) — never from a client-supplied
    header alone. If the kiosk also sends an optional ``X-Tenant-Id`` header, it
    is cross-checked against the device's own tenant; a mismatch indicates a
    misconfigured or rogue terminal and is rejected with 403 rather than
    silently trusting either value.
    """
    tenant_id_header = request.headers.get("X-Tenant-Id")
    if tenant_id_header:
        try:
            header_tenant_id = uuid.UUID(tenant_id_header)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid X-Tenant-Id"
            ) from exc
        if header_tenant_id != device.tenant_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="X-Tenant-Id does not match the authenticated device's tenant",
            )
    return device.tenant_id


# ---------------------------------------------------------------------------
# In-process rate limiting for /nfc/* (device-authenticated) endpoints.
#
# Guards against badge-id enumeration and the is_admin oracle on
# nfc_preview/nfc_profile: a device-key holder could otherwise brute-force
# badge-ids (and learn which ones are admin-flagged) without any throttle.
#
# Deliberately dependency-free (no slowapi/redis) — a simple sliding-window
# counter keyed by (device_id, client-ip) in a module-level dict. NOTE: this
# state is per-process. A multi-process deployment (multiple uvicorn workers
# behind one terminal) would need shared state (e.g. Redis) to enforce a
# single global limit; out of scope for now (see also crypto-protocol TODO
# on nfc_register_admin_cards below for the related SAM-ceremony follow-up).
# ---------------------------------------------------------------------------

_nfc_rate_buckets: dict[str, deque[float]] = defaultdict(deque)


def _rate_limit_bucket_key(device: DeviceContext, request: Request, suffix: str = "") -> str:
    ip = request.client.host if request.client else "unknown"
    return f"{device.device_id}:{ip}:{suffix}"


def _check_rate_limit(key: str, max_requests: int, window_seconds: float) -> None:
    """Raise 429 if *key* has exceeded *max_requests* within the sliding window."""
    now = time.monotonic()
    bucket = _nfc_rate_buckets[key]
    cutoff = now - window_seconds
    while bucket and bucket[0] < cutoff:
        bucket.popleft()
    if len(bucket) >= max_requests:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests — please slow down.",
            headers={"Retry-After": str(int(window_seconds))},
        )
    bucket.append(now)


def _enforce_nfc_rate_limit(device: DeviceContext, request: Request) -> None:
    """General per-(device, ip) throttle applied to every device-authenticated
    ``/nfc/*`` call."""
    settings = get_settings()
    _check_rate_limit(
        _rate_limit_bucket_key(device, request),
        settings.nfc_rate_limit_max_requests,
        settings.nfc_rate_limit_window_seconds,
    )


def _enforce_nfc_failure_rate_limit(device: DeviceContext, request: Request) -> None:
    """Tighter throttle for calls resolving to an unknown badge (404) —
    the primary badge-id-enumeration / is_admin-oracle vector."""
    settings = get_settings()
    _check_rate_limit(
        _rate_limit_bucket_key(device, request, suffix="404"),
        settings.nfc_rate_limit_max_failures,
        settings.nfc_rate_limit_window_seconds,
    )


def _admin_cards(tenant: Tenant) -> set[str]:
    """Admin token-ids for the terminal personalize-mode gate (bead 4c admin).

    Stored in ``tenant.settings["admin_cards"]`` — a revocable, backend-side
    flag (NOT an on-chip attribute). See ``nfc_register_admin_cards`` for how
    tokens are added; revocation is a future web-admin feature.
    """
    settings = tenant.settings or {}
    return set(settings.get("admin_cards") or [])


async def _remember_unknown_card(db: AsyncSession, tenant_id: uuid.UUID, badge_id: str) -> None:
    """Add an unknown badge to the tenant's scanned-card pool for later assignment.

    Deduplicated by UID; admins pick it up via the personnel board. Capped at
    ``settings.nfc_scanned_cards_max`` entries (oldest evicted first) so a
    device-key holder can't grow ``tenant.settings`` unbounded by tapping/
    POSTing a stream of bogus badge-ids.
    """
    tenant = (await db.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one_or_none()
    if tenant is None:
        return
    settings = dict(tenant.settings or {})
    cards = list(settings.get("scanned_cards") or [])
    if not any(c.get("uid") == badge_id for c in cards):
        cards.append({"uid": badge_id, "at": datetime.now(UTC).isoformat()})
        max_size = get_settings().nfc_scanned_cards_max
        if len(cards) > max_size:
            cards = cards[-max_size:]
    settings["scanned_cards"] = cards
    tenant.settings = settings
    await db.commit()


@router.post("/punch", response_model=PunchEventResponse, status_code=status.HTTP_201_CREATED)
async def punch(
    body: PunchRequest,
    db: DBSession,
    current_user: AuthUser,
    request: Request,
) -> PunchEventResponse:
    """Record a clock event for the authenticated employee.

    Requires ``timesheet:view_own`` permission — Buchhaltung and Betriebsrat do NOT
    have this permission and cannot clock in/out (ADR-007).

    Security:
    - event_time is always server-generated (no client timestamps accepted)
    - employee_id comes from the JWT, never from the request body
    - source is restricted to WEB_PORTAL/PWA_MOBILE for non-admin users
    """
    # Guard: only roles with clock-in/out rights may use this endpoint.
    # Buchhaltung (timesheet:view_aggregates) and Betriebsrat (timesheet:view_aggregates +
    # audit:read_anonymized) have no timesheet:view_own and must not punch in.
    if not current_user.has_permission("timesheet:view_own"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="timesheet:view_own permission required to clock in/out",
        )

    # V6 fix: Non-admin users cannot spoof source as ADMIN_MANUAL or SYSTEM_AUTO
    ALLOWED_USER_SOURCES = {EventSource.WEB_PORTAL, EventSource.PWA_MOBILE}
    source = body.source
    if not current_user.has_permission_namespace("admin") and source not in ALLOWED_USER_SOURCES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Source '{source.value}' is not allowed for regular users",
        )

    ip = request.client.host if request.client else None
    try:
        event = await clock_service.record_punch(
            db=db,
            tenant_id=current_user.tenant_id,
            employee_id=current_user.user_id,
            event_type=body.event_type,
            source=source,
            device_id=body.device_id,
            ip_address=ip,
            # event_time intentionally omitted — server always uses UTC now
        )
    except clock_service.InvalidPunchStateError as exc:
        raise api_error(status.HTTP_409_CONFLICT, exc) from exc
    except clock_service.PunchRateLimitError as exc:
        raise api_error(
            status.HTTP_429_TOO_MANY_REQUESTS, exc, headers={"Retry-After": "60"}
        ) from exc
    await db.commit()

    # Fire-and-forget: recompute timesheet via Hatchet workflow.
    # Non-fatal — the timesheet was already recomputed inline by record_punch;
    # the workflow provides async post-processing (ArbZG flag surfacing, etc.).
    try:
        from zeiterfassung.core.hatchet import get_hatchet
        from zeiterfassung.workflows.clock_processing import PunchInput, clock_processing

        _hatchet = get_hatchet()  # noqa: F841 — validates client is up
        clock_processing.run_no_wait(
            PunchInput(
                tenant_id=str(current_user.tenant_id),
                employee_id=str(current_user.user_id),
                work_date=event.event_time.date().isoformat(),
            )
        )
    except Exception:  # noqa: BLE001
        pass  # Non-fatal — workflow dispatch is supplementary

    return PunchEventResponse.model_validate(event)


@router.post(
    "/nfc/preview",
    response_model=dict,
    status_code=status.HTTP_200_OK,
)
async def nfc_preview(
    body: NFCPunchRequest,
    db: DBSession,
    device: DeviceCtx,
    request: Request,
) -> dict:
    """Preview what action a badge scan would take, without executing.

    Returns employee info and the action that would be performed, plus
    ``is_admin`` (bead 4c admin) — true when this token is flagged as an admin
    fob in ``tenant.settings["admin_cards"]``. A dedicated admin fob (not bound
    to any employee's ``nfc_badge_id``) still resolves here rather than 404ing,
    with placeholder employee/state fields the kiosk ignores when
    ``is_admin`` is true; the kiosk uses this to toggle terminal personalize
    mode instead of showing the normal punch menu. A badge that is BOTH an
    assigned employee's own card AND admin-flagged returns the normal employee
    fields with ``is_admin: true`` — the kiosk still treats the tap as an admin
    action (mode toggle takes priority over punching).
    """
    tenant_id = _resolve_device_tenant_id(request, device)
    _enforce_nfc_rate_limit(device, request)

    tenant = (await db.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one_or_none()
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    is_admin = body.badge_id in _admin_cards(tenant)

    try:
        employee = await clock_service.get_employee_by_badge(db, tenant_id, body.badge_id)
    except clock_service.NoBadgeUserError as exc:
        if is_admin:
            # Admin-only fob: not bound to any employee. Placeholder fields —
            # the kiosk must not treat these as a real employee/punch state.
            return {
                "employee_name": "",
                "employee_number": "",
                "badge_id": body.badge_id,
                "current_state": "clocked_out",
                "next_action": "clock_in",
                "is_admin": True,
            }
        _enforce_nfc_failure_rate_limit(device, request)
        await _remember_unknown_card(db, tenant_id, body.badge_id)
        raise api_error(status.HTTP_404_NOT_FOUND, exc) from exc

    clock_status = await clock_service.get_current_clock_status(db, tenant_id, employee.id)
    clocked_in = clock_status.get("is_clocked_in", False)
    on_break = clock_status.get("is_on_break", False)

    if on_break:
        next_action = "break_end"
    elif clocked_in:
        next_action = "clock_out"
    else:
        next_action = "clock_in"

    return {
        "employee_name": f"{employee.first_name} {employee.last_name}",
        "employee_number": employee.employee_number or "",
        "badge_id": body.badge_id,
        "current_state": "on_break" if on_break else "clocked_in" if clocked_in else "clocked_out",
        "next_action": next_action,
        "is_admin": is_admin,
    }


@router.post(
    "/nfc/auto",
    response_model=dict,
    status_code=status.HTTP_200_OK,
)
async def nfc_auto_punch(
    body: NFCPunchRequest,
    db: DBSession,
    device: DeviceCtx,
    request: Request,
) -> dict:
    """Smart NFC punch — auto-detects PUNCH_IN or PUNCH_OUT based on current state.

    Returns employee name and action taken for terminal display.
    """
    tenant_id = _resolve_device_tenant_id(request, device)
    _enforce_nfc_rate_limit(device, request)

    try:
        employee = await clock_service.get_employee_by_badge(db, tenant_id, body.badge_id)
    except clock_service.NoBadgeUserError as exc:
        _enforce_nfc_failure_rate_limit(device, request)
        raise api_error(status.HTTP_404_NOT_FOUND, exc) from exc

    def _auto_response(evt_type: EventType, evt_time: datetime) -> dict:
        return {
            "employee_name": f"{employee.first_name} {employee.last_name}",
            "employee_number": employee.employee_number or "",
            "action": _NFC_ACTION_LABELS.get(evt_type, str(evt_type)),
            "event_time": evt_time.isoformat(),
        }

    # Offline-Queue: Nachreichung mit bekannter client_event_id nicht doppelt buchen.
    if body.client_event_id:
        existing = await clock_service.get_punch_by_client_event_id(
            db, tenant_id, body.client_event_id
        )
        if existing is not None:
            return _auto_response(existing.event_type, existing.event_time)

    # Auto-detect action from current state
    clock_status = await clock_service.get_current_clock_status(db, tenant_id, employee.id)
    clocked_in = clock_status.get("is_clocked_in", False)
    on_break = clock_status.get("is_on_break", False)

    if on_break:
        event_type = EventType.BREAK_END
    elif clocked_in:
        event_type = EventType.PUNCH_OUT
    else:
        event_type = EventType.PUNCH_IN

    ip = request.client.host if request.client else None
    try:
        event = await clock_service.record_punch(
            db=db,
            tenant_id=tenant_id,
            employee_id=employee.id,
            event_type=event_type,
            source=EventSource.NFC_TERMINAL,
            device_id=device.device_id,
            ip_address=ip,
            event_time=_device_event_time(body.event_time),
            client_event_id=body.client_event_id,
        )
    except clock_service.InvalidPunchStateError as exc:
        raise api_error(status.HTTP_409_CONFLICT, exc) from exc
    except clock_service.PunchRateLimitError as exc:
        raise api_error(
            status.HTTP_429_TOO_MANY_REQUESTS, exc, headers={"Retry-After": "60"}
        ) from exc

    try:
        await db.commit()
    except IntegrityError:
        # Race zweier Nachreichungen mit derselben client_event_id.
        await db.rollback()
        if body.client_event_id:
            existing = await clock_service.get_punch_by_client_event_id(
                db, tenant_id, body.client_event_id
            )
            if existing is not None:
                return _auto_response(existing.event_type, existing.event_time)
        raise

    resp = _auto_response(event.event_type, event.event_time)
    resp["personal_messages"] = await _personal_messages(db, tenant_id, employee.id)
    return resp


@router.post(
    "/nfc/undo",
    response_model=dict,
    status_code=status.HTTP_200_OK,
    summary="Undo the last NFC-terminal punch (grace window)",
)
async def nfc_undo(
    body: NFCPunchRequest,
    db: DBSession,
    device: DeviceCtx,
    request: Request,
) -> dict:
    """Delete the employee's most recent terminal punch within the grace window.

    Powers the terminal's "Rückgängig" button for accidental wrong-button
    recovery. Returns the employee name and the action that was undone.
    """
    tenant_id = _resolve_device_tenant_id(request, device)
    _enforce_nfc_rate_limit(device, request)

    try:
        employee = await clock_service.get_employee_by_badge(db, tenant_id, body.badge_id)
    except clock_service.NoBadgeUserError as exc:
        _enforce_nfc_failure_rate_limit(device, request)
        raise api_error(status.HTTP_404_NOT_FOUND, exc) from exc

    try:
        event_type, event_time = await clock_service.undo_last_punch(db, tenant_id, employee.id)
    except clock_service.NothingToUndoError as exc:
        raise api_error(status.HTTP_409_CONFLICT, exc) from exc

    await db.commit()

    action_labels = {
        EventType.PUNCH_IN: "clock_in",
        EventType.PUNCH_OUT: "clock_out",
        EventType.BREAK_START: "break_start",
        EventType.BREAK_END: "break_end",
    }
    return {
        "employee_name": f"{employee.first_name} {employee.last_name}",
        "undone_action": action_labels.get(event_type, str(event_type)),
        "event_time": event_time.isoformat(),
    }


@router.post(
    "/nfc",
    response_model=dict,
    status_code=status.HTTP_201_CREATED,
    summary="NFC terminal punch",
)
async def nfc_punch(
    body: NFCPunchRequest,
    db: DBSession,
    device: DeviceCtx,
    request: Request,
) -> PunchEventResponse:
    """Record a punch event from an NFC terminal using badge ID.

    The terminal authenticates with the X-Device-Api-Key header. The tenant is
    resolved from the device's DB registration (an optional X-Tenant-Id header
    is cross-checked, not trusted on its own — see ``_resolve_device_tenant_id``).
    """
    tenant_id = _resolve_device_tenant_id(request, device)
    _enforce_nfc_rate_limit(device, request)

    try:
        employee = await clock_service.get_employee_by_badge(db, tenant_id, body.badge_id)
    except clock_service.NoBadgeUserError as exc:
        _enforce_nfc_failure_rate_limit(device, request)
        raise api_error(status.HTTP_404_NOT_FOUND, exc) from exc

    # Offline-Queue: Nachreichungen sind idempotent — gleiche client_event_id
    # (vom Terminal generiert) wird nicht doppelt gebucht, sondern der bereits
    # gespeicherte Stempel zurueckgegeben.
    if body.client_event_id:
        existing = await clock_service.get_punch_by_client_event_id(
            db, tenant_id, body.client_event_id
        )
        if existing is not None:
            return {
                "employee_name": f"{employee.first_name} {employee.last_name}",
                "employee_number": employee.employee_number or "",
                "event_type": existing.event_type.value,
                "event_time": existing.event_time.isoformat(),
            }

    ip = request.client.host if request.client else None
    try:
        event = await clock_service.record_punch(
            db=db,
            tenant_id=tenant_id,
            employee_id=employee.id,
            event_type=body.event_type,
            source=EventSource.NFC_TERMINAL,
            device_id=device.device_id,
            ip_address=ip,
            event_time=_device_event_time(body.event_time),
            client_event_id=body.client_event_id,
        )
    except clock_service.InvalidPunchStateError as exc:
        raise api_error(status.HTTP_409_CONFLICT, exc) from exc
    except clock_service.PunchRateLimitError as exc:
        raise api_error(
            status.HTTP_429_TOO_MANY_REQUESTS, exc, headers={"Retry-After": "60"}
        ) from exc
    try:
        await db.commit()
    except IntegrityError:
        # Race zweier gleichzeitiger Nachreichungen mit derselben client_event_id:
        # der Unique-Index gewinnt — den bereits gebuchten Stempel zurueckgeben.
        await db.rollback()
        if body.client_event_id:
            existing = await clock_service.get_punch_by_client_event_id(
                db, tenant_id, body.client_event_id
            )
            if existing is not None:
                return {
                    "employee_name": f"{employee.first_name} {employee.last_name}",
                    "employee_number": employee.employee_number or "",
                    "event_type": existing.event_type.value,
                    "event_time": existing.event_time.isoformat(),
                }
        raise
    return {
        "employee_name": f"{employee.first_name} {employee.last_name}",
        "employee_number": employee.employee_number or "",
        "event_type": event.event_type.value,
        "event_time": event.event_time.isoformat(),
        "personal_messages": await _personal_messages(db, tenant_id, employee.id),
    }


async def _personal_messages(
    db: DBSession, tenant_id: uuid.UUID, employee_id: uuid.UUID
) -> list[str]:
    """Aktive persoenliche Kiosk-Meldungen fuer die Stempel-Antwort (#189)."""
    from zeiterfassung.services import kiosk_message_service

    tenant = await db.get(Tenant, tenant_id)
    today = tenant_local_date(datetime.now(tz=UTC), tenant)
    return await kiosk_message_service.active_for_employee(db, tenant_id, employee_id, today)


@router.get("/kiosk-messages", response_model=dict)
async def kiosk_broadcasts(db: DBSession, device: DeviceCtx, request: Request) -> dict:
    """Aktive Rundmeldungen fuer den Terminal-Ruhebildschirm (device-auth).

    Das Terminal ruft dies periodisch ab; persoenliche Meldungen kommen
    dagegen mit der Stempel-Antwort (siehe ``_personal_messages``).
    """
    tenant_id = _resolve_device_tenant_id(request, device)
    from zeiterfassung.services import kiosk_message_service

    tenant = await db.get(Tenant, tenant_id)
    today = tenant_local_date(datetime.now(tz=UTC), tenant)
    return {"messages": await kiosk_message_service.active_broadcasts(db, tenant_id, today)}


@router.post(
    "/nfc/profile",
    response_model=dict,
    status_code=status.HTTP_200_OK,
    summary="Employee profile for the NFC terminal (device-authenticated)",
)
async def nfc_profile(
    body: NFCPunchRequest,
    db: DBSession,
    device: DeviceCtx,
    request: Request,
) -> dict:
    """Return live profile data for a badge so the kiosk can show the employee's
    own web-profile summary directly on the terminal display.

    Powers the terminal's "Profil anschauen" button. Device-authenticated
    (``X-Device-Api-Key``, tenant resolved from the device registration) — no
    user login, so we call the services directly with the badge-resolved
    employee. Surfaces the current clock state, today's live minutes
    (recomputed like ``/status``), the running Arbeitszeitkonto balance and the
    remaining vacation for the year.
    """
    tenant_id = _resolve_device_tenant_id(request, device)
    _enforce_nfc_rate_limit(device, request)

    try:
        employee = await clock_service.get_employee_by_badge(db, tenant_id, body.badge_id)
    except clock_service.NoBadgeUserError as exc:
        _enforce_nfc_failure_rate_limit(device, request)
        await _remember_unknown_card(db, tenant_id, body.badge_id)
        raise api_error(status.HTTP_404_NOT_FOUND, exc) from exc

    # Current clock state + live minutes for today (recompute like /status does so
    # in-progress work is reflected).
    clock_status = await clock_service.get_current_clock_status(db, tenant_id, employee.id)
    # Tenant-lokales "heute", nicht Server-/UTC-Datum (#3).
    tenant = await db.get(Tenant, tenant_id)
    today = tenant_local_date(datetime.now(tz=UTC), tenant)
    ts = await clock_service.recompute_daily_timesheet(db, tenant_id, employee.id, today)
    await db.commit()

    on_break = clock_status.get("is_on_break", False)
    clocked_in = clock_status.get("is_clocked_in", False)
    current_state = "on_break" if on_break else "clocked_in" if clocked_in else "clocked_out"

    # Ueberstunden-Konto aus dem Ledger (#148) — dieselbe Zahl wie in der
    # Web-Ansicht und im Export. (Das alte MonthlySummary-Feld
    # arbeitszeitkonto_balance_minutes befuellt nur seed_demo.)
    from zeiterfassung.services import overtime_service

    try:
        bal_hours = await overtime_service.live_balance(db, tenant_id, employee)
        konto_balance = int(round(float(bal_hours) * 60))
    except Exception:  # noqa: BLE001 — Konto ist Zusatzinfo, Profil nie blockieren
        konto_balance = 0

    # Remaining vacation for the current year. Supplementary — never fail the
    # profile if the balance cannot be computed.
    vacation: dict | None = None
    try:
        balance = await absence_service.get_vacation_balance(db, tenant_id, employee.id, today.year)
        vacation = {
            "entitlement_days": balance.entitlement_days,
            "used_days": balance.used_days,
            "pending_days": balance.pending_days,
            "remaining_days": balance.remaining_days,
        }
    except Exception:  # noqa: BLE001 — vacation balance is optional for the display
        vacation = None

    return {
        "employee_name": f"{employee.first_name} {employee.last_name}",
        "employee_number": employee.employee_number or "",
        "current_state": current_state,
        "today_start": ts.start_time.isoformat() if ts and ts.start_time else None,
        "today_end": ts.end_time.isoformat() if ts and ts.end_time else None,
        "today_work_minutes": ts.net_work_minutes if ts else 0,
        "today_break_minutes": ts.break_minutes if ts else 0,
        "today_overtime_minutes": ts.overtime_minutes if ts else 0,
        "arbeitszeitkonto_balance_minutes": konto_balance,
        "contracted_hours_per_week": employee.contracted_hours_per_week,
        "vacation": vacation,
    }


@router.post(
    "/nfc/register-to-pool",
    response_model=RegisterToPoolResponse,
    status_code=status.HTTP_200_OK,
    summary="Batch-register freshly personalized token-ids into the scan pool",
)
async def nfc_register_to_pool(
    body: RegisterToPoolRequest,
    db: DBSession,
    device: DeviceCtx,
    request: Request,
) -> RegisterToPoolResponse:
    """Pool freshly personalized DESFire token-ids reported by the on-terminal
    provisioning script (run over SSH during setup), so they show up in the
    personnel board (``GET /admin/scanned-cards``) immediately, without a
    per-fob terminal tap.

    Device-authenticated the same way as the other ``/nfc/*`` endpoints
    (``X-Device-Api-Key``): the tenant is resolved from the device's DB
    registration, never from a client-supplied header alone. An optional
    ``X-Tenant-Id`` header is only cross-checked (403 on mismatch, 400 on
    malformed) — see ``_resolve_device_tenant_id``. Idempotent: re-posting the
    same batch does not duplicate pool entries, and a token already assigned
    to an active employee (``User.nfc_badge_id``) is never pooled. Only
    mutates ``tenant.settings`` — never touches ``punch_events``/ledger rows.

    Chip-authenticity (SAM) is verified by the caller (kiosk-side, bead bi0);
    this endpoint only performs device-key auth + idempotent pooling.

    Optional ``body.uids`` (bead 8t0) carries the physical, non-secret chip UID
    (readable via any phone's NFC reader) parallel-indexed to ``token_ids``, so
    an admin can later match a phone-read UID to the right pooled token in the
    web app. Stored as a new ``chip_uid`` field on the pool entry — the
    existing ``uid`` field (which holds the *token*, not the chip UID) is left
    untouched for frontend compatibility. If a token is already pooled without
    a ``chip_uid``, a later call carrying one backfills it in place (no
    duplicate entry); an existing ``chip_uid`` is never overwritten.
    """
    tenant_id = _resolve_device_tenant_id(request, device)
    _enforce_nfc_rate_limit(device, request)

    tenant = (await db.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one_or_none()
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    assigned_uids = {
        r[0]
        for r in (
            await db.execute(
                tenant_scoped(select(User.nfc_badge_id), User, tenant_id).where(
                    User.is_active.is_(True),
                    User.nfc_badge_id.isnot(None),
                )
            )
        ).all()
    }

    settings = dict(tenant.settings or {})
    # Copy each entry into a fresh dict (not just a fresh list of the *same*
    # nested dict objects): the backfill branch below mutates an entry
    # in-place, and if that entry were still the same object referenced by
    # the currently-loaded `tenant.settings`, the old/new comparison
    # SQLAlchemy does at flush time would see identical content (since the
    # "old" value got silently mutated too) and skip the UPDATE entirely.
    cards = [dict(c) for c in (settings.get("scanned_cards") or [])]
    existing_uids = {c.get("uid") for c in cards}

    chip_uids = body.uids if body.uids is not None else [None] * len(body.token_ids)

    pooled = 0
    skipped_existing = 0
    skipped_assigned = 0
    backfilled = 0
    now_iso = datetime.now(UTC).isoformat()
    for token_id, chip_uid in zip(body.token_ids, chip_uids, strict=True):
        if token_id in assigned_uids:
            skipped_assigned += 1
            continue
        if token_id in existing_uids:
            skipped_existing += 1
            # Backfill (bead 8t0): a fob already pooled may not have had its
            # chip-UID captured yet — a later report can add it, but never
            # overwrite one that's already recorded.
            if chip_uid:
                for c in cards:
                    if c.get("uid") == token_id and not c.get("chip_uid"):
                        c["chip_uid"] = chip_uid
                        backfilled += 1
                        break
            continue
        entry: dict[str, str] = {"uid": token_id}
        if chip_uid:
            entry["chip_uid"] = chip_uid
        entry["at"] = now_iso
        cards.append(entry)
        existing_uids.add(token_id)
        pooled += 1

    settings["scanned_cards"] = cards
    tenant.settings = settings
    await db.commit()

    pool_size = sum(1 for c in cards if c.get("uid") not in assigned_uids)

    return RegisterToPoolResponse(
        pooled=pooled,
        skipped_existing=skipped_existing,
        skipped_assigned=skipped_assigned,
        pool_size=pool_size,
        backfilled=backfilled,
    )


@router.post(
    "/nfc/register-admin-cards",
    response_model=RegisterAdminCardsResponse,
    status_code=status.HTTP_200_OK,
    summary="Flag freshly personalized token-ids as admin fobs",
)
async def nfc_register_admin_cards(
    body: RegisterAdminCardsRequest,
    db: DBSession,
    device: DeviceCtx,
    request: Request,
) -> RegisterAdminCardsResponse:
    """Mark token-ids as admin fobs (terminal personalize-mode gate).

    Called by the on-terminal provisioning script (``sam_personalize --admin``)
    after personalizing a fob intended to unlock the wall terminal's
    personalize mode. Device-authenticated the same way as the other
    ``/nfc/*`` endpoints. Idempotent: re-posting an already-flagged token is a
    no-op. Admin fobs are stored in ``tenant.settings["admin_cards"]`` and are
    NEVER left in the scanned-cards pool (``/nfc/register-to-pool``) — they
    must not be assignable as an employee badge. Only mutates
    ``tenant.settings`` — never touches ``punch_events``/ledger rows.

    Hardening: a token-id must already be "known" to the tenant before it can
    be flagged admin — either genuinely personalized+pooled via
    ``/nfc/register-to-pool``, or already an active employee's assigned badge
    (a supervisor's own punch badge doubling as an admin fob). Without this,
    any device-key holder could flag an arbitrary made-up string as an admin
    fob (chip-authenticity/SAM-ceremony bypass). Unknown token-ids are
    rejected with 400 rather than silently skipped. A freshly-flagged token
    is removed from the general scanned-cards pool so it can no longer be
    picked up as an employee candidate.

    TODO(SAM-ceremony): this is a pool-membership check, not proof the caller
    actually ran the SAM personalize ceremony for *this specific* token —
    see ``crypto-protocol-unknown-verify-reference-before-declaring-nda.md``
    and the admin-fob-purpose rule for the full design; tracked as follow-up.
    """
    tenant_id = _resolve_device_tenant_id(request, device)
    _enforce_nfc_rate_limit(device, request)

    tenant = (await db.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one_or_none()
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    settings = dict(tenant.settings or {})
    admin_cards = list(settings.get("admin_cards") or [])
    existing = set(admin_cards)

    scanned_cards = list(settings.get("scanned_cards") or [])
    scanned_uids = {c.get("uid") for c in scanned_cards}
    assigned_uids = {
        r[0]
        for r in (
            await db.execute(
                tenant_scoped(select(User.nfc_badge_id), User, tenant_id).where(
                    User.is_active.is_(True),
                    User.nfc_badge_id.isnot(None),
                )
            )
        ).all()
    }
    known_uids = scanned_uids | assigned_uids | existing

    unknown = [t for t in body.token_ids if t not in known_uids]
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "token_id(s) not recognized — personalize + register-to-pool "
                f"(or assign as an employee badge) first: {', '.join(unknown)}"
            ),
        )

    added = 0
    already_admin = 0
    for token_id in body.token_ids:
        if token_id in existing:
            already_admin += 1
            continue
        admin_cards.append(token_id)
        existing.add(token_id)
        added += 1

    # Newly (or previously) flagged admin tokens must never remain assignable
    # via the general scanned-cards pool.
    token_id_set = set(body.token_ids)
    settings["scanned_cards"] = [c for c in scanned_cards if c.get("uid") not in token_id_set]
    settings["admin_cards"] = admin_cards
    tenant.settings = settings
    await db.commit()

    return RegisterAdminCardsResponse(
        added=added,
        already_admin=already_admin,
        admin_count=len(admin_cards),
    )


@router.get("/events", response_model=list[dict])
async def get_own_events(
    date_from: date,
    date_to: date,
    db: DBSession,
    current_user: AuthUser,
) -> list[dict]:
    """Raw punch events (the actual activity log) for the caller in a date range."""
    if not current_user.has_permission("timesheet:view_own"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="timesheet:view_own required"
        )
    start = datetime(date_from.year, date_from.month, date_from.day, tzinfo=UTC)
    end = datetime(date_to.year, date_to.month, date_to.day, 23, 59, 59, 999999, tzinfo=UTC)
    rows = (
        (
            await db.execute(
                tenant_scoped(select(PunchEvent), PunchEvent, current_user.tenant_id)
                .where(
                    PunchEvent.employee_id == current_user.user_id,
                    PunchEvent.event_time >= start,
                    PunchEvent.event_time <= end,
                )
                .order_by(PunchEvent.event_time, PunchEvent.id)
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "event_type": e.event_type.value,
            "event_time": e.event_time.isoformat(),
            "source": e.source.value,
        }
        for e in rows
    ]


@router.post(
    "/nfc/leave-request",
    response_model=dict,
    status_code=status.HTTP_201_CREATED,
    summary="Submit a vacation request from the NFC terminal (device-authenticated)",
)
async def nfc_leave_request(
    body: NFCLeaveRequest,
    db: DBSession,
    device: DeviceCtx,
    request: Request,
) -> dict:
    """File a VACATION request for the badge-resolved employee from the terminal.

    Powers the terminal's "Urlaub beantragen" action (for colleagues without a
    PC). Device-authenticated; the request type is forced to VACATION and the
    request is created as SUBMITTED (never auto-approved from a kiosk), so it
    shows up in the manager's approval queue. days_count (working days) is
    computed server-side.
    """
    tenant_id = _resolve_device_tenant_id(request, device)
    _enforce_nfc_rate_limit(device, request)

    try:
        employee = await clock_service.get_employee_by_badge(db, tenant_id, body.badge_id)
    except clock_service.NoBadgeUserError as exc:
        _enforce_nfc_failure_rate_limit(device, request)
        raise api_error(status.HTTP_404_NOT_FOUND, exc) from exc

    try:
        data = AbsenceRequestCreate(
            request_type=RequestType.VACATION,
            date_from=body.date_from,
            date_to=body.date_to,
            notes=body.notes or "Urlaubsantrag am Terminal",
        )
    except ValueError as exc:
        # date_to < date_from, or a start date in the past.
        raise api_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY, exc, code=ErrorCode.LEAVE_REQUEST_VALIDATION
        ) from exc

    try:
        req = await absence_service.create_request(db, tenant_id, employee.id, data)
    except absence_service.ConflictError as exc:
        raise api_error(status.HTTP_409_CONFLICT, exc) from exc
    await db.commit()

    # Remaining vacation for the year, best-effort, so the terminal can show it.
    remaining_days = None
    try:
        balance = await absence_service.get_vacation_balance(
            db, tenant_id, employee.id, body.date_from.year
        )
        remaining_days = balance.remaining_days
    except Exception:  # noqa: BLE001 — supplementary, never fail the confirmation
        remaining_days = None

    return {
        "employee_name": f"{employee.first_name} {employee.last_name}",
        "employee_number": employee.employee_number,
        "request_type": req.request_type.value,
        "status": req.status.value,
        "date_from": req.date_from.isoformat(),
        "date_to": req.date_to.isoformat(),
        "days_count": float(req.days_count),
        "vacation_remaining_days": remaining_days,
    }


@router.get("/timesheet", response_model=list[DailyTimesheetResponse])
async def get_own_timesheet(
    date_from: date,
    date_to: date,
    db: DBSession,
    current_user: AuthUser,
) -> list[DailyTimesheetResponse]:
    """Return timesheets scoped to the caller's data visibility.

    - Global (GF/HR): all timesheets for the tenant in the date range
    - Department (Teamleiter): timesheets for their department
    - Self (Mitarbeiter): own timesheets only
    - Anonymized (Betriebsrat): empty list — use /br/overtime-stats instead
    """
    scope = current_user.get_scope()
    # Auto-Erfassung: fehlende Werktage bis heute mit Tages-Soll füllen.
    await clock_service.ensure_auto_timesheets(
        db, current_user.tenant_id, current_user.user_id, date_from, date_to
    )
    timesheets = await clock_service.get_timesheets(
        db, current_user.tenant_id, current_user.user_id, date_from, date_to, scope=scope
    )
    return [DailyTimesheetResponse.model_validate(t) for t in timesheets]


@router.get("/timesheet/{employee_id}", response_model=list[DailyTimesheetResponse])
async def get_employee_timesheet(
    employee_id: uuid.UUID,
    date_from: date,
    date_to: date,
    db: DBSession,
    current_user: AuthUser,
) -> list[DailyTimesheetResponse]:
    """Return timesheets for a specific employee.

    Permission rules:
    - ``timesheet:view_all`` (GF/HR): any employee in the tenant
    - ``timesheet:view_team`` (Teamleiter): only employees in same department
    - Own ID: always allowed (self-service) provided the role has ``timesheet:view_own``
    - Old exact ``admin``/``clock:all`` permissions accepted for backward compatibility

    Buchhaltung (``timesheet:view_aggregates``) and Betriebsrat may NOT use this
    endpoint to access individual employee timesheets — they must use the aggregate
    endpoints under ``/br/`` instead (ADR-007).
    Note: ``has_permission_namespace("admin")`` is intentionally NOT used — it would
    allow ``admin:dashboard`` (Buchhaltung) to bypass this guard.
    """
    # Short-circuit: always allow viewing own timesheets (if role permits clock access)
    if employee_id == current_user.user_id:
        if not current_user.has_permission("timesheet:view_own"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="timesheet:view_own permission required",
            )
        await clock_service.ensure_auto_timesheets(
            db, current_user.tenant_id, employee_id, date_from, date_to
        )
        timesheets = await clock_service.get_timesheets(
            db, current_user.tenant_id, employee_id, date_from, date_to
        )
        return [DailyTimesheetResponse.model_validate(t) for t in timesheets]

    has_view_all = (
        current_user.has_permission("timesheet:view_all")
        or current_user.has_permission("admin")
        or current_user.has_permission("clock:all")
    )
    has_view_team = current_user.has_permission("timesheet:view_team")

    if not has_view_all and not has_view_team:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient permissions to view another employee's timesheets",
        )

    if has_view_team and not has_view_all:
        # Department scope: verify the target employee is in the same department
        if not current_user.department_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No department assigned to your account",
            )
        from sqlalchemy import select

        from zeiterfassung.models.user import User

        result = await db.execute(
            tenant_scoped(select(User), User, current_user.tenant_id).where(
                User.id == employee_id,
                User.department_id.in_(current_user.department_ids),
            )
        )
        if result.scalar_one_or_none() is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Target employee is not in your department",
            )

    await clock_service.ensure_auto_timesheets(
        db, current_user.tenant_id, employee_id, date_from, date_to
    )
    timesheets = await clock_service.get_timesheets(
        db, current_user.tenant_id, employee_id, date_from, date_to
    )
    return [DailyTimesheetResponse.model_validate(t) for t in timesheets]


@router.get("/status", response_model=ClockStatusResponse)
async def get_clock_status(
    db: DBSession,
    current_user: AuthUser,
    employee_id: uuid.UUID | None = None,
) -> ClockStatusResponse:
    """Current clock-in/out status.

    Without ``employee_id``: the authenticated employee's own status (needs
    ``timesheet:view_own``). With ``employee_id``: a specific employee's status —
    allowed for admins/HR/Teamleiter (timesheet:view_all/view_team, users:read_all
    or admin namespace), so the Mitarbeiterprofil can show "eingestempelt seit".
    """
    if employee_id is not None:
        can_other = (
            current_user.has_permission("timesheet:view_all")
            or current_user.has_permission("timesheet:view_team")
            or current_user.has_permission("users:read_all")
            or current_user.has_permission_namespace("admin")
        )
        if not can_other:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="permission required to view another employee's status",
            )
        target_id = employee_id
    else:
        if not current_user.has_permission("timesheet:view_own"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="timesheet:view_own permission required",
            )
        target_id = current_user.user_id
        if target_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Account has no linked employee",
            )

    clock_status = await clock_service.get_current_clock_status(
        db, current_user.tenant_id, target_id
    )

    # Recompute timesheet for today to get live minutes (includes in-progress work).
    # Tenant-lokales "heute", nicht Server-/UTC-Datum (#3).
    tenant = await db.get(Tenant, current_user.tenant_id)
    today = tenant_local_date(datetime.now(tz=UTC), tenant)
    ts = await clock_service.recompute_daily_timesheet(db, current_user.tenant_id, target_id, today)

    last_event = clock_status["last_event"]
    return ClockStatusResponse(
        is_clocked_in=clock_status["is_clocked_in"],
        is_on_break=clock_status["is_on_break"],
        last_event=PunchEventResponse.model_validate(last_event) if last_event else None,
        clocked_in_since=clock_status["clocked_in_since"],
        break_started_at=clock_status["break_started_at"],
        today_work_minutes=ts.net_work_minutes if ts else 0,
        today_break_minutes=ts.break_minutes if ts else 0,
        today_overtime_minutes=ts.overtime_minutes if ts else 0,
    )


@router.post("/recompute/{work_date}", response_model=DailyTimesheetResponse)
async def recompute_timesheet(
    work_date: date,
    employee_id: uuid.UUID,
    db: DBSession,
    admin: AdminUser,
) -> DailyTimesheetResponse:
    """Force recomputation of a daily timesheet from raw punch events.

    Requires ``timesheet:view_all`` (GF/HR) in addition to the ``AdminUser`` dependency.
    The ``AdminUser`` dependency uses ``has_permission_namespace("admin")`` which also
    matches ``admin:dashboard`` (Buchhaltung). The explicit ``timesheet:view_all`` guard
    ensures only GF/HR can trigger recomputation (ADR-007).
    """
    if not admin.has_permission("timesheet:view_all") and not admin.has_permission("admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="timesheet:view_all permission required",
        )
    ts = await clock_service.recompute_daily_timesheet(db, admin.tenant_id, employee_id, work_date)
    await db.commit()
    return DailyTimesheetResponse.model_validate(ts)


# ---------------------------------------------------------------------------
# Manuelle Korrekturen am Tages-Log (für Mitarbeiter mit Korrektur-Recht).
# Alle Aktionen sind append-only Events; das Roh-Log wird nie überschrieben.
# Gate: timesheet:edit_all (alle) bzw. timesheet:edit_team (eigene Abteilung).
# ---------------------------------------------------------------------------


async def _assert_can_correct(db: AsyncSession, user: CurrentUser, employee_id: uuid.UUID) -> None:
    """Authorize a timesheet correction for *employee_id*.

    Global editors (``timesheet:edit_all`` / admin) may edit anyone in the
    tenant; ``timesheet:edit_team`` only employees in their own department.
    """
    has_global = user.has_permission("timesheet:edit_all") or user.has_permission_namespace("admin")
    if has_global:
        return
    if user.has_permission("timesheet:edit_team"):
        if user.department_id is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No department assigned to your account",
            )
        res = await db.execute(
            tenant_scoped(select(User), User, user.tenant_id).where(
                User.id == employee_id,
                User.department_id == user.department_id,
            )
        )
        if res.scalar_one_or_none() is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Target employee is not in your department",
            )
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Keine Berechtigung für Zeitkorrekturen (timesheet:edit_all/edit_team)",
    )


async def _day_detail(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    employee_id: uuid.UUID,
    work_date: date,
) -> DayDetailResponse:
    """Assemble the day-log + frisch berechnetem Timesheet für die UI.

    Frisch rechnen statt gespeicherten Stand lesen: so stimmt "Arbeit" auch bei
    laufender Schicht (Live-Minuten) — ohne Commit bleibt nichts dauerhaft.
    """
    events = await clock_service.get_day_events(db, tenant_id, employee_id, work_date)
    ts = await clock_service.recompute_daily_timesheet(db, tenant_id, employee_id, work_date)
    return DayDetailResponse(
        work_date=work_date,
        events=[DayEventEntry(**e) for e in events],
        timesheet=DailyTimesheetResponse.model_validate(ts) if ts else None,
    )


@router.get("/day-events/{employee_id}", response_model=DayDetailResponse)
async def get_day_detail(
    employee_id: uuid.UUID,
    work_date: date,
    db: DBSession,
    current_user: AuthUser,
) -> DayDetailResponse:
    """Return a single day's punch log (with correction history) + timesheet."""
    await _assert_can_correct(db, current_user, employee_id)
    return await _day_detail(db, current_user.tenant_id, employee_id, work_date)


@router.patch("/auto-day", response_model=DailyTimesheetResponse)
async def set_auto_day(body: dict, db: DBSession, current_user: AuthUser) -> DailyTimesheetResponse:
    """Tages-Arbeitszeit direkt setzen (Auto-Erfassung): Kommt/Geht/Pause ohne Stempel.

    Body: ``employee_id``, ``work_date`` (YYYY-MM-DD), ``start``/``end`` (HH:MM), ``break_minutes``.
    """
    from datetime import date as _date

    try:
        employee_id = uuid.UUID(str(body["employee_id"]))
        work_date = _date.fromisoformat(str(body["work_date"]))
        start = str(body["start"])
        end = str(body["end"])
        break_minutes = int(body.get("break_minutes", 0) or 0)
    except (KeyError, ValueError, TypeError) as err:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Ungültige Eingabe."
        ) from err

    await _assert_can_correct(db, current_user, employee_id)
    try:
        ts = await clock_service.set_manual_day(
            db,
            current_user.tenant_id,
            employee_id,
            work_date,
            start,
            end,
            break_minutes,
            actor_id=current_user.user_id,
        )
    except (ValueError, clock_service.InvalidPunchStateError) as e:
        raise api_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY, e, code=ErrorCode.MANUAL_DAY_VALIDATION
        ) from e
    await db.commit()
    await db.refresh(ts)
    return DailyTimesheetResponse.model_validate(ts)


@router.patch("/day-unworked", response_model=DailyTimesheetResponse)
async def set_day_unworked(
    body: dict, db: DBSession, current_user: AuthUser
) -> DailyTimesheetResponse:
    """Tag als „nicht gearbeitet" markieren (leerer Tagesbogen → unentschuldigtes Fehlen).

    Body: ``employee_id``, ``work_date`` (YYYY-MM-DD).
    """
    from datetime import date as _date

    try:
        employee_id = uuid.UUID(str(body["employee_id"]))
        work_date = _date.fromisoformat(str(body["work_date"]))
    except (KeyError, ValueError, TypeError) as err:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Ungültige Eingabe."
        ) from err

    await _assert_can_correct(db, current_user, employee_id)
    try:
        ts = await clock_service.set_day_unworked(
            db, current_user.tenant_id, employee_id, work_date, actor_id=current_user.user_id
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)) from e
    await db.commit()
    await db.refresh(ts)
    return DailyTimesheetResponse.model_validate(ts)


@router.patch("/weekend-booking", response_model=DailyTimesheetResponse)
async def set_weekend_booking(
    body: dict, db: DBSession, current_user: AuthUser
) -> DailyTimesheetResponse:
    """Verbuchung von Wochenend-/Feiertagsarbeit für EINEN Tag festlegen.

    Body: ``employee_id``, ``work_date`` (YYYY-MM-DD),
    ``booking`` = "ueberstunden" (Default: zählt nicht ins Konto, wird direkt
    vergütet) oder "gleitzeit" (zählt ins Überstundenkonto).
    Nur für Samstage/Sonntage/Feiertage; gleiche Rechte wie Stempel-Korrekturen.
    """
    from datetime import date as _date

    try:
        employee_id = uuid.UUID(str(body["employee_id"]))
        work_date = _date.fromisoformat(str(body["work_date"]))
        booking = str(body["booking"]).lower()
    except (KeyError, ValueError, TypeError) as err:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Ungültige Eingabe."
        ) from err
    if booking not in ("ueberstunden", "gleitzeit"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="booking muss 'ueberstunden' oder 'gleitzeit' sein.",
        )

    await _assert_can_correct(db, current_user, employee_id)

    from zeiterfassung.services.tenant_service import resolve_bundesland

    bundesland = await resolve_bundesland(db, current_user.tenant_id)
    ts_row = (
        await db.execute(
            tenant_scoped(select(DailyTimesheet), DailyTimesheet, current_user.tenant_id).where(
                DailyTimesheet.employee_id == employee_id,
                DailyTimesheet.work_date == work_date,
            )
        )
    ).scalar_one_or_none()
    if ts_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Für diesen Tag existiert kein Tagesbogen.",
        )
    if not (
        work_date.weekday() >= 5 or is_holiday(work_date, bundesland) or ts_row.is_holiday_work
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Verbuchung ist nur für Wochenend-/Feiertagsarbeit wählbar.",
        )
    # NULL = Default (Überstunden) — explizites "ueberstunden" ebenfalls als
    # NULL speichern, damit Profil-/Default-Logik eindeutig bleibt.
    ts_row.weekend_booking = "gleitzeit" if booking == "gleitzeit" else None
    ts_row.updated_at = datetime.now(tz=UTC)
    await db.commit()
    await db.refresh(ts_row)
    return DailyTimesheetResponse.model_validate(ts_row)


@router.patch("/break-override", response_model=DailyTimesheetResponse)
async def set_break_override(
    body: dict, db: DBSession, current_user: AuthUser
) -> DailyTimesheetResponse:
    """Pausen-Aufschlag eines Tages festlegen (HR-Korrektur der Pausenregel).

    Body: ``employee_id``, ``work_date`` (YYYY-MM-DD), ``minutes`` = Delta zu
    den gestempelten Pausen (0–480) oder ``null`` = zurück zur Automatik.
    Serverseitig gespiegelt: keine negativen Werte (UI-Grenze ist kein Schutz).
    Gleiche Rechte wie Stempel-Korrekturen; der Tagesbogen wird sofort neu
    berechnet, Roh-Events bleiben unangetastet.
    """
    from datetime import date as _date

    try:
        employee_id = uuid.UUID(str(body["employee_id"]))
        work_date = _date.fromisoformat(str(body["work_date"]))
        raw_minutes = body.get("minutes")
        minutes = None if raw_minutes is None else int(raw_minutes)
    except (KeyError, ValueError, TypeError) as err:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Ungültige Eingabe."
        ) from err
    if minutes is not None and not 0 <= minutes <= 480:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="minutes muss zwischen 0 und 480 liegen (kein negativer Aufschlag).",
        )

    await _assert_can_correct(db, current_user, employee_id)

    ts_row = (
        await db.execute(
            tenant_scoped(select(DailyTimesheet), DailyTimesheet, current_user.tenant_id).where(
                DailyTimesheet.employee_id == employee_id,
                DailyTimesheet.work_date == work_date,
            )
        )
    ).scalar_one_or_none()
    if ts_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Für diesen Tag existiert kein Tagesbogen.",
        )
    ts_row.break_auto_override_minutes = minutes
    ts_row.updated_at = datetime.now(tz=UTC)
    await db.flush()
    ts = await clock_service.recompute_daily_timesheet(
        db, current_user.tenant_id, employee_id, work_date
    )
    await db.commit()
    await db.refresh(ts)
    return DailyTimesheetResponse.model_validate(ts)


@router.post(
    "/manual-event",
    response_model=DayDetailResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_manual_event(
    body: ManualEventRequest,
    db: DBSession,
    current_user: AuthUser,
) -> DayDetailResponse:
    """Add a forgotten/manual event (e.g. clock-in or clock-out) for an employee."""
    await _assert_can_correct(db, current_user, body.employee_id)
    try:
        ev = await clock_service.add_manual_event(
            db,
            current_user.tenant_id,
            body.employee_id,
            body.event_type,
            body.event_time,
            current_user.user_id,
            body.reason,
            created_by_account_id=current_user.account_id,
        )
    except clock_service.InvalidPunchStateError as exc:
        raise api_error(status.HTTP_422_UNPROCESSABLE_ENTITY, exc) from exc
    await db.commit()
    return await _day_detail(db, current_user.tenant_id, body.employee_id, ev.event_time.date())


@router.post(
    "/manual-break",
    response_model=DayDetailResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_manual_break(
    body: ManualBreakRequest,
    db: DBSession,
    current_user: AuthUser,
) -> DayDetailResponse:
    """Add a break as a von–bis pair. Multiple breaks per day are supported."""
    await _assert_can_correct(db, current_user, body.employee_id)
    try:
        await clock_service.add_break(
            db,
            current_user.tenant_id,
            body.employee_id,
            body.start_time,
            body.end_time,
            current_user.user_id,
            body.reason,
            created_by_account_id=current_user.account_id,
        )
    except clock_service.InvalidPunchStateError as exc:
        raise api_error(status.HTTP_422_UNPROCESSABLE_ENTITY, exc) from exc
    await db.commit()
    return await _day_detail(db, current_user.tenant_id, body.employee_id, body.start_time.date())


@router.post("/events/{event_id}/correct", response_model=DayDetailResponse)
async def correct_punch_event(
    event_id: int,
    body: CorrectEventRequest,
    db: DBSession,
    current_user: AuthUser,
) -> DayDetailResponse:
    """Replace an event's time (appends a superseding correction; original stays)."""
    try:
        orig = await clock_service._load_event(db, current_user.tenant_id, event_id)
    except clock_service.EventNotFoundError as exc:
        raise api_error(status.HTTP_404_NOT_FOUND, exc) from exc
    await _assert_can_correct(db, current_user, orig.employee_id)
    try:
        new_ev = await clock_service.correct_event(
            db,
            current_user.tenant_id,
            event_id,
            body.event_time,
            current_user.user_id,
            body.reason,
            created_by_account_id=current_user.account_id,
        )
    except clock_service.InvalidPunchStateError as exc:
        raise api_error(status.HTTP_422_UNPROCESSABLE_ENTITY, exc) from exc
    await db.commit()
    return await _day_detail(db, current_user.tenant_id, orig.employee_id, new_ev.event_time.date())


@router.post("/events/{event_id}/void", response_model=DayDetailResponse)
async def void_punch_event(
    event_id: int,
    body: VoidEventRequest,
    db: DBSession,
    current_user: AuthUser,
) -> DayDetailResponse:
    """Remove an event (appends a void marker that supersedes it; original stays)."""
    try:
        orig = await clock_service._load_event(db, current_user.tenant_id, event_id)
    except clock_service.EventNotFoundError as exc:
        raise api_error(status.HTTP_404_NOT_FOUND, exc) from exc
    await _assert_can_correct(db, current_user, orig.employee_id)
    await clock_service.void_event(
        db,
        current_user.tenant_id,
        event_id,
        current_user.user_id,
        body.reason,
        created_by_account_id=current_user.account_id,
    )
    await db.commit()
    return await _day_detail(db, current_user.tenant_id, orig.employee_id, orig.event_time.date())


@router.get("/presence", response_model=PresenceOverviewResponse)
async def get_presence(
    db: DBSession,
    current_user: AuthUser,
    status_filter: str | None = None,
) -> PresenceOverviewResponse:
    """Return live presence status for employees visible to the caller.

    - ``timesheet:view_all`` (GF/HR): all employees in the tenant
    - ``timesheet:view_team`` (Teamleiter): employees in their department only

    Optional ``status_filter`` query param: comma-separated statuses to include,
    e.g. ``?status_filter=clocked_in,on_break``. Valid values:
    ``clocked_in``, ``on_break``, ``clocked_out``, ``vacation``, ``sick_leave``.

    Deliberately excludes ``admin:dashboard`` and ``timesheet:view_aggregates``
    to prevent Buchhaltung and Betriebsrat from accessing individual presence
    data (ADR-007).
    """
    has_view_all = current_user.has_permission("timesheet:view_all")
    has_view_team = current_user.has_permission("timesheet:view_team")

    if not has_view_all and not has_view_team:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="timesheet:view_all or timesheet:view_team permission required",
        )

    scope = current_user.get_scope()
    data = await clock_service.get_presence_overview(db, current_user.tenant_id, scope)

    employees = [EmployeePresence(**emp) for emp in data["employees"]]

    # Apply status filter if provided
    valid_statuses = {"clocked_in", "on_break", "clocked_out", "vacation", "sick_leave"}
    if status_filter:
        requested = {s.strip() for s in status_filter.split(",") if s.strip() in valid_statuses}
        if requested:
            employees = [e for e in employees if e.status in requested]

    return PresenceOverviewResponse(
        total_employees=data["total_employees"],
        present_count=data["present_count"],
        on_break_count=data["on_break_count"],
        vacation_count=data["vacation_count"],
        sick_leave_count=data["sick_leave_count"],
        employees=employees,
        departments=[DepartmentPresenceSummary(**d) for d in data["departments"]],
    )


@router.get("/punch-log/{employee_id}", response_model=list[dict])
async def punch_log(
    employee_id: uuid.UUID,
    date_from: date,
    date_to: date,
    db: DBSession,
    current_user: AuthUser,
) -> list[dict]:
    """Unveraenderbares Stempel-Protokoll eines Mitarbeiters (neueste zuerst).

    Liefert ALLE Roh-Ereignisse im Zeitraum — Kiosk-Stempel wie manuelle
    Korrekturen/Stornos — ungefiltert. ``punch_events`` ist append-only;
    Korrekturen sind neue Zeilen mit ``corrects_event_id``, Stornos nur
    ``is_void``-Marker. ``effective`` zeigt, ob das Event heute noch in die
    Tagesberechnung eingeht (gleiche Logik wie recompute).

    Rechte: eigener Mitarbeiter ODER timesheet:view_all/view_team,
    users:read_all, admin-Namespace (wie GET /clock/status mit employee_id).
    """
    from zeiterfassung.services.tenant_service import tenant_timezone

    is_self = current_user.user_id is not None and employee_id == current_user.user_id
    can_other = (
        current_user.has_permission("timesheet:view_all")
        or current_user.has_permission("timesheet:view_team")
        or current_user.has_permission("users:read_all")
        or current_user.has_permission_namespace("admin")
    )
    if not is_self and not can_other:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="permission required to view another employee's punch log",
        )
    if date_to < date_from:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="date_to muss >= date_from sein",
        )
    if (date_to - date_from).days > 366:
        # Bewusst harte Grenze statt stillem Limit (Bug-Klasse #163/#165).
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Zeitraum zu gross - maximal 366 Tage pro Abfrage",
        )

    # Tenant-lokale Tagesgrenzen (Muster recompute_daily_timesheet, #3).
    tenant = await db.get(Tenant, current_user.tenant_id)
    tz = tenant_timezone(tenant)
    span_start = datetime(date_from.year, date_from.month, date_from.day, tzinfo=tz).astimezone(UTC)
    span_end = (
        datetime(date_to.year, date_to.month, date_to.day, tzinfo=tz) + timedelta(days=1)
    ).astimezone(UTC)

    result = await db.execute(
        select(PunchEvent)
        .where(
            PunchEvent.tenant_id == current_user.tenant_id,
            PunchEvent.employee_id == employee_id,
            PunchEvent.event_time >= span_start,
            PunchEvent.event_time < span_end,
        )
        .order_by(PunchEvent.event_time.desc(), PunchEvent.id.desc())
    )
    events = list(result.scalars().all())

    superseded = await clock_service._superseded_ids(  # noqa: SLF001 — gleiche Projektion wie recompute
        db, current_user.tenant_id, employee_id, [e.id for e in events]
    )

    # Namen der Korrektur-Autoren (nur die tatsaechlich vorkommenden).
    author_ids = {e.created_by for e in events if e.created_by}
    authors: dict[uuid.UUID, str] = {}
    if author_ids:
        rows = await db.execute(
            tenant_scoped(select(User), User, current_user.tenant_id).where(User.id.in_(author_ids))
        )
        authors = {u.id: u.full_name for u in rows.scalars().all()}

    # Ausfuehrende Login-Konten (auch Admin-Konten ohne Mitarbeiter, #169).
    # Altbestand vor Migration 0038 hat hier NULL.
    from zeiterfassung.models.account import Account as _Account

    acct_ids = {e.created_by_account_id for e in events if e.created_by_account_id}
    acct_emails: dict[uuid.UUID, str] = {}
    if acct_ids:
        rows = await db.execute(
            tenant_scoped(select(_Account), _Account, current_user.tenant_id).where(
                _Account.id.in_(acct_ids)
            )
        )
        acct_emails = {a.id: a.email for a in rows.scalars().all()}

    # Terminal-Name statt roher Geraete-UUID: punch_events.device_id haelt
    # str(Device.id); die Quelle-Spalte soll "Kiosk · Halle" zeigen, nicht die
    # 36-stellige ID. Unbekannte/geloeschte Geraete bleiben None — das UI faellt
    # dann auf eine gekuerzte ID zurueck, damit nichts verloren geht.
    device_uuids: set[uuid.UUID] = set()
    for e in events:
        if e.device_id:
            try:
                device_uuids.add(uuid.UUID(e.device_id))
            except ValueError:
                continue
    device_names: dict[str, str] = {}
    if device_uuids:
        rows = await db.execute(
            tenant_scoped(select(Device), Device, current_user.tenant_id).where(
                Device.id.in_(device_uuids)
            )
        )
        device_names = {str(d.id): d.name for d in rows.scalars().all()}

    return [
        {
            "id": e.id,
            "event_time": e.event_time,
            "created_at": e.created_at,
            "event_type": e.event_type.value if hasattr(e.event_type, "value") else e.event_type,
            "source": e.source.value if hasattr(e.source, "value") else e.source,
            "device_id": e.device_id,
            "device_name": device_names.get(e.device_id) if e.device_id else None,
            "is_void": e.is_void,
            "corrects_event_id": e.corrects_event_id,
            "reason": e.reason,
            "created_by_name": authors.get(e.created_by) if e.created_by else None,
            "created_by_account_email": (
                acct_emails.get(e.created_by_account_id) if e.created_by_account_id else None
            ),
            "effective": e.id not in superseded and not e.is_void,
        }
        for e in events
    ]
