"""Typed API error codes — decouples the HTTP error contract from internal
exception wording.

Problem (zeiterfassung-rmeb, code review finding cja): several endpoints raised
``HTTPException(detail=str(exc))``, turning whatever text an internal service
exception happened to carry into the public API contract. A harmless wording
change in a service (e.g. rephrasing a German validation message) would then
silently change what clients see and could break string-matching integrations.

Fix: every service-layer error that crosses the API boundary gets a stable
:class:`ErrorCode` member. Routers build the HTTP response via :func:`api_error`,
which always emits ``detail = {"code": <stable enum value>, "message": <text>}``.
``code`` is the part of the contract clients should match on; ``message`` is a
human-readable, presentation-safe string that MAY still change wording between
releases without being a breaking change.

Usage in a router::

    try:
        ...
    except clock_service.InvalidPunchStateError as exc:
        raise api_error(status.HTTP_409_CONFLICT, exc) from exc

``api_error`` resolves the code from ``exc.code`` when *exc* is a
:class:`DomainError` (or any exception exposing a ``code: ErrorCode``
attribute). For exceptions outside our hierarchy (stdlib ``ValueError``,
``LookupError``, ``PermissionError``, third-party errors such as
``jwt.InvalidTokenError``) the call site must pass ``code=`` explicitly::

    except LookupError as exc:
        raise api_error(status.HTTP_404_NOT_FOUND, exc, code=ErrorCode.SICK_MAIL_NOT_FOUND) from exc

This module intentionally does NOT touch router control-flow (permission
checks, status-code selection, endpoint structure) — only how the error
``detail`` body is constructed. See ``.claude/rules`` for the router-structure
follow-up (bead 6pcv), which is out of scope here.
"""

from __future__ import annotations

import enum
from typing import ClassVar

from fastapi import HTTPException


class ErrorCode(enum.StrEnum):
    """Stable, typed error codes exposed in API error responses.

    ``detail.code`` is the part of the response clients may safely match on.
    Values are deliberately snake_case strings (not integers) so they read
    sensibly in API docs/logs and survive reordering without renumbering.
    """

    # Generic fallback — should not normally appear; indicates a call site
    # that raised through :func:`api_error` without an explicit/derivable code.
    INTERNAL = "internal_error"

    # --- auth / tokens (api/deps.py) ------------------------------------
    INVALID_TOKEN = "invalid_token"

    # --- clock / punch events (services/clock_service.py) ---------------
    INVALID_PUNCH_STATE = "invalid_punch_state"
    PUNCH_RATE_LIMITED = "punch_rate_limited"
    NO_BADGE_USER = "no_badge_user"
    NOTHING_TO_UNDO = "nothing_to_undo"
    EVENT_NOT_FOUND = "event_not_found"
    MANUAL_DAY_VALIDATION = "manual_day_validation"

    # --- absences / leave requests (services/absence_service.py) --------
    ABSENCE_REQUEST_NOT_FOUND = "absence_request_not_found"
    ABSENCE_CONFLICT = "absence_conflict"
    ABSENCE_INVALID_STATUS_TRANSITION = "absence_invalid_status_transition"
    ABSENCE_SELF_APPROVAL = "absence_self_approval"
    VACATION_BALANCE_FORBIDDEN = "vacation_balance_forbidden"
    LEAVE_REQUEST_VALIDATION = "leave_request_validation"

    # --- sick-mail inbox (services/sickmail_service.py) ------------------
    SICK_MAIL_NOT_FOUND = "sick_mail_not_found"
    SICK_MAIL_CONFLICT = "sick_mail_conflict"
    SICK_MAIL_FETCH_FAILED = "sick_mail_fetch_failed"

    # --- users / accounts (services/user_service.py) --------------------
    DUPLICATE_EMAIL = "duplicate_email"
    DEPARTMENT_COMPANY_MISMATCH = "department_company_mismatch"
    ROLE_NOT_FOUND = "role_not_found"
    WORK_TIME_PROFILE_MISMATCH = "work_time_profile_mismatch"

    # --- payroll export (services/export_service.py) --------------------
    EXPORT_NOT_FOUND = "export_not_found"
    EXPORT_VALIDATION = "export_validation_error"


class DomainError(Exception):
    """Base class for service-layer errors that carry a stable API error code.

    Subclasses set a class-level ``code``. The exception message itself stays
    a normal, human-readable ``str(exc)`` — it is presentation text, not part
    of the contract; ``code`` is. See module docstring.
    """

    code: ClassVar[ErrorCode] = ErrorCode.INTERNAL


def api_error(
    status_code: int,
    exc: Exception,
    *,
    code: ErrorCode | None = None,
    headers: dict[str, str] | None = None,
) -> HTTPException:
    """Build the typed :class:`HTTPException` for *exc*, preserving *status_code*.

    ``detail`` is always ``{"code": ..., "message": ...}`` — never a raw
    ``str(exc)`` used as the entire response body. *code* is resolved, in
    order: the explicit *code* argument, then ``exc.code`` (set by
    :class:`DomainError` subclasses), then :attr:`ErrorCode.INTERNAL` as a
    last resort (callers outside our hierarchy should always pass *code*
    explicitly rather than rely on this fallback).
    """
    resolved = code if code is not None else getattr(exc, "code", None)
    if resolved is None:
        resolved = ErrorCode.INTERNAL
    return HTTPException(
        status_code=status_code,
        detail={"code": resolved.value, "message": str(exc)},
        headers=headers,
    )
