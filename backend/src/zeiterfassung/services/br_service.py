"""Betriebsrat service — anonymized aggregate functions only.

All functions in this module return aggregate statistics with no employee
names or identifiable information, in accordance with BetrVG §87 and
BDSG §26.  Individual employee data MUST NOT be returned here.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from zeiterfassung.models.absence import ApprovalRequest, RequestStatus, RequestType
from zeiterfassung.models.audit import AuditLog
from zeiterfassung.models.clock import DailyTimesheet
from zeiterfassung.models.department import Department
from zeiterfassung.models.user import User


async def get_department_overtime_stats(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[dict]:
    """Return per-department aggregate overtime statistics.

    Returns a list of dicts with keys:
    - ``department_id``: UUID of the department (or ``None`` for unassigned)
    - ``headcount``: number of active employees
    - ``avg_work_minutes_per_day``: tenant-wide average across the period
    - ``avg_overtime_minutes_per_day``: average daily overtime

    No employee names or individual records are included.
    """
    # Base timesheet query scoped to tenant, with department name
    stmt = (
        select(
            User.department_id,
            Department.name.label("department_name"),
            func.count(func.distinct(User.id)).label("headcount"),
            func.avg(DailyTimesheet.net_work_minutes).label("avg_work_minutes"),
            func.avg(DailyTimesheet.overtime_minutes).label("avg_overtime_minutes"),
        )
        .join(DailyTimesheet, DailyTimesheet.employee_id == User.id)
        .outerjoin(Department, Department.id == User.department_id)
        .where(
            User.tenant_id == tenant_id,
            User.is_active == True,  # noqa: E712
            DailyTimesheet.tenant_id == tenant_id,
        )
    )

    if date_from is not None:
        stmt = stmt.where(DailyTimesheet.work_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(DailyTimesheet.work_date <= date_to)

    stmt = stmt.group_by(User.department_id, Department.name)
    result = await db.execute(stmt)
    rows = result.all()

    return [
        {
            "department_id": str(row.department_id) if row.department_id else None,
            "department_name": row.department_name or "Nicht zugeordnet",
            "headcount": row.headcount,
            "avg_work_minutes_per_day": round(float(row.avg_work_minutes or 0), 1),
            "avg_overtime_minutes_per_day": round(float(row.avg_overtime_minutes or 0), 1),
        }
        for row in rows
    ]


async def get_absence_statistics(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    year: int | None = None,
) -> dict:
    """Return tenant-wide absence aggregate statistics.

    Returns a dict with keys:
    - ``total_sick_days``: sum of approved sick leave days
    - ``total_vacation_days``: sum of approved vacation days
    - ``avg_sick_days_per_employee``: mean sick days per active employee
    - ``avg_vacation_days_per_employee``: mean vacation days per active employee
    - ``active_employee_count``: number of active employees used as denominator

    No employee-level data is included.
    """
    # Count active employees for averages
    emp_count_result = await db.execute(
        select(func.count())
        .select_from(User)
        .where(User.tenant_id == tenant_id, User.is_active == True)  # noqa: E712
    )
    active_count: int = emp_count_result.scalar_one() or 1  # avoid div-by-zero

    # Base absence filter
    base_where = [
        ApprovalRequest.tenant_id == tenant_id,
        ApprovalRequest.status == RequestStatus.APPROVED,
    ]
    if year is not None:
        year_start = date(year, 1, 1)
        year_end = date(year, 12, 31)
        base_where += [
            ApprovalRequest.date_from >= year_start,
            ApprovalRequest.date_from <= year_end,
        ]

    sick_result = await db.execute(
        select(func.coalesce(func.sum(ApprovalRequest.days_count), 0)).where(
            *base_where,
            ApprovalRequest.request_type == RequestType.SICK_LEAVE,
        )
    )
    total_sick = float(sick_result.scalar_one() or 0)

    vac_result = await db.execute(
        select(func.coalesce(func.sum(ApprovalRequest.days_count), 0)).where(
            *base_where,
            ApprovalRequest.request_type == RequestType.VACATION,
        )
    )
    total_vacation = float(vac_result.scalar_one() or 0)

    return {
        "active_employee_count": active_count,
        "total_sick_days": round(total_sick, 1),
        "total_vacation_days": round(total_vacation, 1),
        "avg_sick_days_per_employee": round(total_sick / active_count, 2),
        "avg_vacation_days_per_employee": round(total_vacation / active_count, 2),
    }


async def get_correction_audit_trail(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    skip: int = 0,
    limit: int = 100,
) -> list[dict]:
    """Return timesheet correction audit log entries with actor ROLE, not name.

    Each entry contains:
    - ``action``: the audit action string (e.g. ``timesheet.correction``)
    - ``resource_type``: type of resource modified
    - ``resource_id``: ID of the modified resource (no PII)
    - ``created_at``: timestamp of the change
    - ``actor_role``: role label for the actor (``admin`` | ``system`` | ``user``)
      — never the actor's name or ID

    Returns only ``timesheet``-related entries to stay within BR's mandate.
    """
    result = await db.execute(
        select(AuditLog)
        .where(
            AuditLog.tenant_id == tenant_id,
            AuditLog.resource_type.in_(["timesheet", "daily_timesheet", "punch_event"]),
        )
        .order_by(AuditLog.created_at.desc())
        .offset(skip)
        .limit(limit)
    )
    logs = result.scalars().all()

    def _actor_role_label(log: AuditLog) -> str:
        """Return a role label without exposing actor identity."""
        if log.actor_id is None:
            return "system"
        details = log.details or {}
        role = details.get("actor_role") or details.get("role")
        if role:
            return str(role)
        # Fallback: distinguish admin vs user actions by action prefix
        if log.action.startswith("admin.") or log.action.startswith("export."):
            return "admin"
        return "user"

    return [
        {
            "action": log.action,
            "resource_type": log.resource_type,
            "resource_id": log.resource_id,
            "created_at": log.created_at.isoformat() if log.created_at else None,
            "actor_role": _actor_role_label(log),
        }
        for log in logs
    ]
