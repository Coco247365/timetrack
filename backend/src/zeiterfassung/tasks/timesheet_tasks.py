"""Background tasks for timesheet recomputation and monthly summary aggregation."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from zeiterfassung.models.absence import ApprovalRequest, RequestStatus, RequestType
from zeiterfassung.models.clock import DailyTimesheet, MonthlySummary
from zeiterfassung.services.clock_service import recompute_daily_timesheet


async def recompute_month(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    employee_id: uuid.UUID,
    year: int,
    month: int,
) -> None:
    """Recompute all daily timesheets and the monthly summary for the given month."""
    from calendar import monthrange

    _, last_day = monthrange(year, month)
    for day in range(1, last_day + 1):
        work_date = date(year, month, day)
        await recompute_daily_timesheet(db, tenant_id, employee_id, work_date)

    await aggregate_monthly_summary(db, tenant_id, employee_id, year, month)


async def aggregate_monthly_summary(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    employee_id: uuid.UUID,
    year: int,
    month: int,
) -> MonthlySummary:
    """Aggregate daily timesheets into a MonthlySummary row."""
    from calendar import monthrange

    year_month = date(year, month, 1)
    _, last_day = monthrange(year, month)
    month_end = date(year, month, last_day)

    # Fetch all daily timesheets for the month
    result = await db.execute(
        select(DailyTimesheet).where(
            DailyTimesheet.tenant_id == tenant_id,
            DailyTimesheet.employee_id == employee_id,
            DailyTimesheet.work_date >= year_month,
            DailyTimesheet.work_date <= month_end,
        )
    )
    timesheets = result.scalars().all()

    # Fetch approved absence requests overlapping the month
    absences_result = await db.execute(
        select(ApprovalRequest).where(
            ApprovalRequest.tenant_id == tenant_id,
            ApprovalRequest.employee_id == employee_id,
            ApprovalRequest.date_from <= month_end,
            ApprovalRequest.date_to >= year_month,
            ApprovalRequest.status == RequestStatus.APPROVED,
            ApprovalRequest.request_type.in_([RequestType.VACATION, RequestType.SICK_LEAVE]),
        )
    )
    absences = absences_result.scalars().all()

    # Aggregate timesheet data
    total_work_minutes = sum(t.net_work_minutes for t in timesheets)
    total_overtime_minutes = sum(t.overtime_minutes for t in timesheets)
    days_worked = sum(1 for t in timesheets if t.net_work_minutes > 0)
    night_supplement_minutes = sum(t.net_work_minutes for t in timesheets if t.is_night_work)
    sunday_supplement_minutes = sum(t.net_work_minutes for t in timesheets if t.is_sunday_work)
    holiday_supplement_minutes = sum(t.net_work_minutes for t in timesheets if t.is_holiday_work)

    # Count absence days
    days_sick = 0
    days_vacation = 0
    for absence in absences:
        from zeiterfassung.core.holidays import working_days_between

        days = working_days_between(
            max(absence.date_from, year_month),
            min(absence.date_to, month_end),
        )
        if absence.request_type == RequestType.SICK_LEAVE:
            days_sick += days
        elif absence.request_type == RequestType.VACATION:
            days_vacation += days

    now = datetime.now(tz=UTC)

    # Upsert monthly summary
    existing_result = await db.execute(
        select(MonthlySummary).where(
            MonthlySummary.tenant_id == tenant_id,
            MonthlySummary.employee_id == employee_id,
            MonthlySummary.year_month == year_month,
        )
    )
    summary = existing_result.scalar_one_or_none()

    if summary is None:
        summary = MonthlySummary(
            tenant_id=tenant_id,
            employee_id=employee_id,
            year_month=year_month,
            created_at=now,
            updated_at=now,
        )
        db.add(summary)

    summary.total_work_minutes = total_work_minutes
    summary.total_overtime_minutes = total_overtime_minutes
    summary.days_worked = days_worked
    summary.days_sick = days_sick
    summary.days_vacation = days_vacation
    summary.days_holiday = sum(1 for t in timesheets if t.is_holiday_work)
    summary.night_supplement_minutes = night_supplement_minutes
    summary.sunday_supplement_minutes = sunday_supplement_minutes
    summary.holiday_supplement_minutes = holiday_supplement_minutes
    summary.updated_at = now

    await db.flush()
    return summary
