"""Betriebsrat (works council) API endpoints — anonymized aggregates only.

All endpoints in this module return aggregate statistics with no employee
names or identifiable personal data, in accordance with BetrVG §87 and
BDSG §26.

Access requires the ``timesheet:view_aggregates`` or ``audit:read_anonymized``
permissions, which are only granted to the ``betriebsrat`` role.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from zeiterfassung.api.deps import AuthUser, DBSession
from zeiterfassung.services import br_service

router = APIRouter(prefix="/br", tags=["betriebsrat"])


def _require_br_role(current_user: AuthUser) -> None:
    """Raise 403 unless the caller is a Betriebsrat member.

    Checks both:
    1. ``timesheet:view_aggregates`` permission (held by betriebsrat role)
    2. ``role_type == "betriebsrat"`` to prevent Buchhaltung (which also holds
       ``timesheet:view_aggregates`` for export purposes) from accessing BR-only
       anonymized aggregate endpoints (ADR-007).

    Buchhaltung accesses aggregate data through ``/admin/monthly-report`` and
    ``/export/datev/`` — not through the BR endpoints.
    """
    if not current_user.has_permission("timesheet:view_aggregates"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="timesheet:view_aggregates permission required",
        )
    if current_user.role_type != "betriebsrat":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Betriebsrat role required for BR endpoints",
        )


def _require_aggregates_permission(current_user: AuthUser) -> None:
    """Alias kept for internal use — prefer _require_br_role for new code."""
    _require_br_role(current_user)


def _require_anonymized_audit_permission(current_user: AuthUser) -> None:
    """Raise 403 unless the caller holds ``audit:read_anonymized``."""
    if not current_user.has_permission("audit:read_anonymized"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="audit:read_anonymized permission required",
        )


class DepartmentOvertimeStat(BaseModel):
    department_id: str | None
    department_name: str = "Nicht zugeordnet"
    headcount: int
    avg_work_minutes_per_day: float
    avg_overtime_minutes_per_day: float


class AbsenceStatistics(BaseModel):
    active_employee_count: int
    total_sick_days: float
    total_vacation_days: float
    avg_sick_days_per_employee: float
    avg_vacation_days_per_employee: float


class AnonymizedAuditEntry(BaseModel):
    action: str
    resource_type: str | None
    resource_id: str | None
    created_at: str | None
    actor_role: str


@router.get("/overtime-stats", response_model=list[DepartmentOvertimeStat])
async def get_overtime_stats(
    db: DBSession,
    current_user: AuthUser,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[DepartmentOvertimeStat]:
    """Return per-department anonymous overtime aggregates.

    Returns average work and overtime minutes per department without
    identifying any individual employee.

    Requires Betriebsrat role (``timesheet:view_aggregates`` + ``role_type=betriebsrat``).
    Buchhaltung also holds ``timesheet:view_aggregates`` for export purposes but must
    NOT access BR-specific endpoints — use ``_require_br_role`` instead of the bare
    permission check (ADR-007).
    """
    _require_br_role(current_user)
    stats = await br_service.get_department_overtime_stats(
        db, current_user.tenant_id, date_from, date_to
    )
    return [DepartmentOvertimeStat(**s) for s in stats]


@router.get("/absence-stats", response_model=AbsenceStatistics)
async def get_absence_stats(
    db: DBSession,
    current_user: AuthUser,
    year: int | None = None,
) -> AbsenceStatistics:
    """Return tenant-wide anonymous absence aggregates.

    Returns total and average sick/vacation days across the tenant without
    identifying individual employees.

    Requires Betriebsrat role (``timesheet:view_aggregates`` + ``role_type=betriebsrat``).
    """
    _require_br_role(current_user)
    stats = await br_service.get_absence_statistics(db, current_user.tenant_id, year)
    return AbsenceStatistics(**stats)


@router.get("/audit-trail", response_model=list[AnonymizedAuditEntry])
async def get_audit_trail(
    db: DBSession,
    current_user: AuthUser,
    skip: int = 0,
    # Default-Limit: Liste ist gekappt (neueste zuerst) — Konsument muss
    # paginieren oder server-seitig filtern (Bug-Klasse #163/#165).
    limit: int = 100,
) -> list[AnonymizedAuditEntry]:
    """Return timesheet correction audit log with actor role, not actor name.

    Returns only timesheet-related audit entries.  Each entry shows the
    action, resource, timestamp, and the role category of the actor
    (``admin`` | ``user`` | ``system``) — never the actor's identity.

    Requires Betriebsrat role (``audit:read_anonymized`` + ``role_type=betriebsrat``).
    The ``role_type`` check is defense-in-depth since ``audit:read_anonymized`` is
    already BR-exclusive per ADR-007 permission matrix.
    """
    _require_br_role(current_user)
    _require_anonymized_audit_permission(current_user)
    entries = await br_service.get_correction_audit_trail(db, current_user.tenant_id, skip, limit)
    return [AnonymizedAuditEntry(**e) for e in entries]
