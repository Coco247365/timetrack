"""Admin dashboard, flagged timesheets, bulk approval, reports, audit log.

Split out of the former monolithic ``api/v1/admin.py`` (zeiterfassung-6pcv).
Mounted under ``/admin`` (prefix applied once by ``api/v1/router.py`` via the
``admin`` package aggregator — see ``api/v1/admin/__init__.py``).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status

from zeiterfassung.api.deps import AuthUser, DBSession
from zeiterfassung.schemas.absence import AbsenceRequestResponse
from zeiterfassung.schemas.admin_reports import (
    ApprovalContext,
    AuditLogEntry,
    BulkApproveRequest,
    DashboardStats,
    EmployeeMonthSummary,
    EmployeeYearReport,
    MonthlyTimesheet,
    MonthReconciliation,
)
from zeiterfassung.schemas.clock import DailyTimesheetResponse
from zeiterfassung.services import report_service

router = APIRouter()


def _has_view_all(admin: AuthUser) -> bool:
    return (
        admin.has_permission("timesheet:view_all")
        or admin.has_permission("admin:dashboard")
        or admin.has_permission_namespace("admin")
        or admin.has_permission("reports:view")
    )


@router.get("/approval-context/{request_id}", response_model=ApprovalContext)
async def get_approval_context(
    request_id: uuid.UUID,
    db: DBSession,
    admin: AuthUser,
) -> ApprovalContext:
    """Return enriched context for a leave request to help approvers make informed decisions.

    Requires ``leave:approve_all`` or ``leave:approve_team``.
    Deliberately excludes ``admin:dashboard`` to prevent Buchhaltung from accessing this
    endpoint — Buchhaltung has no approval authority (ADR-007).
    """
    if not (
        admin.has_permission("leave:approve_all") or admin.has_permission("leave:approve_team")
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions"
        )
    return await report_service.approval_context(db, admin.tenant_id, request_id)


@router.get("/dashboard", response_model=DashboardStats)
async def dashboard(db: DBSession, admin: AuthUser) -> DashboardStats:
    """Return high-level dashboard statistics.

    Requires ``admin:dashboard`` or legacy ``admin`` permission.
    """
    if not admin.has_permission("admin:dashboard") and not admin.has_permission_namespace("admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions"
        )
    return await report_service.dashboard_stats(db, admin.tenant_id)


@router.get("/flagged", response_model=list[DailyTimesheetResponse])
async def get_flagged_timesheets(
    db: DBSession,
    admin: AuthUser,
    skip: int = 0,
    # Default-Limit: Liste ist gekappt (neueste zuerst) — Konsument muss
    # paginieren oder server-seitig filtern (Bug-Klasse #163/#165).
    limit: int = 50,
) -> list[DailyTimesheetResponse]:
    """List all flagged timesheets requiring admin attention.

    Requires ``timesheet:view_all`` (GF/HR) or legacy exact ``admin`` permission.
    Buchhaltung (``admin:dashboard``) is excluded because flagged timesheets contain
    identifiable per-employee data; Buchhaltung may only access aggregates (ADR-007).
    """
    # ADR-007: buchhaltung has admin:dashboard but must NOT see per-employee flagged timesheets.
    # Use explicit timesheet:view_all check instead of admin:dashboard.
    has_view_all = admin.has_permission("timesheet:view_all") or admin.has_permission("admin")
    if not has_view_all:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions"
        )
    return await report_service.flagged_timesheets(db, admin.tenant_id, skip, limit)


@router.get("/pending-approvals", response_model=list[AbsenceRequestResponse])
async def get_pending_approvals(
    db: DBSession,
    admin: AuthUser,
    skip: int = 0,
    # Default-Limit: Liste ist gekappt (neueste zuerst) — Konsument muss
    # paginieren oder server-seitig filtern (Bug-Klasse #163/#165).
    limit: int = 50,
) -> list[AbsenceRequestResponse]:
    """List pending leave approval requests with employee names.

    - ``leave:approve_all`` (GF/HR): all pending requests
    - ``leave:approve_team`` (Teamleiter): pending requests in their department

    Deliberately excludes ``admin:dashboard`` to prevent Buchhaltung from seeing
    approval queues — Buchhaltung has no approval authority (ADR-007).
    """
    if not (
        admin.has_permission("leave:approve_all") or admin.has_permission("leave:approve_team")
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions"
        )
    # Use scope so Teamleiter sees only their department's pending requests
    scope = admin.get_scope()
    return await report_service.pending_approvals(db, admin.tenant_id, scope, skip, limit)


@router.post("/bulk-approve", response_model=list[AbsenceRequestResponse])
async def bulk_approve(
    body: BulkApproveRequest,
    db: DBSession,
    admin: AuthUser,
) -> list[AbsenceRequestResponse]:
    """Approve multiple leave requests in one call.

    Requires ``leave:approve_all`` (GF/HR) only. Teamleiter uses the single-request
    endpoint with department scope check. Legacy exact ``admin`` permission also accepted.
    ``has_permission_namespace`` is NOT used here to prevent ``admin:dashboard``
    (Buchhaltung) from triggering this path (ADR-007).
    """
    if not admin.has_permission("leave:approve_all") and not admin.has_permission("admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions"
        )
    return await report_service.bulk_approve(
        db, admin.tenant_id, body.request_ids, admin.user_id, body.reason
    )


@router.get("/monthly-report", response_model=list[EmployeeMonthSummary])
async def monthly_report(
    db: DBSession,
    admin: AuthUser,
    year: int,
    month: int,
) -> list[EmployeeMonthSummary]:
    """Per-employee summary for a given month (for admin reports page).

    - ``timesheet:view_all`` or ``admin:dashboard`` (GF/HR/Buchhaltung): all employees
    - ``timesheet:view_team`` (Teamleiter): employees in their department only
    """
    has_view_all = _has_view_all(admin)
    has_view_team = admin.has_permission("timesheet:view_team")
    if not has_view_all and not has_view_team:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions"
        )
    return await report_service.monthly_report(
        db,
        admin.tenant_id,
        year,
        month,
        has_view_all=has_view_all,
        has_view_team=has_view_team,
        caller_department_id=admin.department_id,
        caller_department_ids=admin.department_ids,
    )


@router.get("/yearly-report", response_model=EmployeeYearReport)
async def yearly_report(
    db: DBSession,
    admin: AuthUser,
    year: int,
    employee_id: uuid.UUID,
) -> EmployeeYearReport:
    """Jahresauswertung fuer EINEN Mitarbeiter: 12 Monatszeilen + Abwesenheiten.

    Rechte wie ``/monthly-report``: view_all/admin/reports sehen alle,
    Teamleiter (timesheet:view_team) nur den eigenen Bereich.
    Abwesenheitstage werden pro Monat geclippt (Arbeitstage im Monat ×
    Halbtag-Fraktion) — ein Krankschein ueber den Monatswechsel zaehlt also
    anteilig in beiden Monaten.
    """
    has_view_all = _has_view_all(admin)
    has_view_team = admin.has_permission("timesheet:view_team")
    if not has_view_all and not has_view_team:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions"
        )
    return await report_service.yearly_report(
        db,
        admin.tenant_id,
        year,
        employee_id,
        has_view_all=has_view_all,
        has_view_team=has_view_team,
        caller_department_id=admin.department_id,
        caller_department_ids=admin.department_ids,
    )


@router.get("/monthly-timesheet", response_model=MonthlyTimesheet)
async def monthly_timesheet(
    db: DBSession,
    admin: AuthUser,
    year: int,
    month: int,
    employee_id: uuid.UUID,
) -> MonthlyTimesheet:
    """Monats-Stundenzettel fuer EINEN Mitarbeiter (wie das alte Altsystem-Blatt).

    Rechte wie ``/yearly-report``: view_all/admin/reports sehen alle,
    Teamleiter (timesheet:view_team) nur den eigenen Bereich.
    """
    has_view_all = _has_view_all(admin)
    has_view_team = admin.has_permission("timesheet:view_team")
    if not has_view_all and not has_view_team:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions"
        )
    return await report_service.monthly_timesheet(
        db,
        admin.tenant_id,
        year,
        month,
        employee_id,
        has_view_all=has_view_all,
        has_view_team=has_view_team,
        caller_department_id=admin.department_id,
        caller_department_ids=admin.department_ids,
    )


@router.get("/month-reconciliation", response_model=MonthReconciliation)
async def month_reconciliation(
    db: DBSession,
    admin: AuthUser,
    year: int,
    month: int,
    employee_id: uuid.UUID,
) -> MonthReconciliation:
    """Monatsabgleich fuer EINEN Mitarbeiter: Soll-Kette, Ueberstunden,
    Zuschlaege und die Lohnarten-Zeilen des Exports (Altsystem-Fussblock).

    Rechte wie ``/monthly-timesheet``.
    """
    has_view_all = _has_view_all(admin)
    has_view_team = admin.has_permission("timesheet:view_team")
    if not has_view_all and not has_view_team:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions"
        )
    return await report_service.month_reconciliation(
        db,
        admin.tenant_id,
        year,
        month,
        employee_id,
        has_view_all=has_view_all,
        has_view_team=has_view_team,
        caller_department_id=admin.department_id,
        caller_department_ids=admin.department_ids,
    )


@router.get("/audit-log", response_model=list[AuditLogEntry])
async def get_audit_log(
    db: DBSession,
    admin: AuthUser,
    skip: int = 0,
    # Default-Limit: Liste ist gekappt (neueste zuerst) — Konsument muss
    # paginieren oder server-seitig filtern (Bug-Klasse #163/#165).
    limit: int = 100,
) -> list[AuditLogEntry]:
    """Return the full audit log for the tenant.

    Requires ``audit:read`` (GF/HR) or legacy exact ``admin`` permission.
    Betriebsrat must use ``GET /br/audit-trail`` which returns anonymized entries.
    Buchhaltung (which has ``admin:dashboard``) must NOT access the full audit log —
    ``has_permission_namespace`` is intentionally NOT used here (ADR-007).
    """
    if not admin.has_permission("audit:read") and not admin.has_permission("admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions"
        )
    return await report_service.audit_log(db, admin.tenant_id, skip, limit)
