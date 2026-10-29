"""Clock processing workflow.

Triggered after a punch event is committed.  Recomputes the employee's daily
timesheet and flags any ArbZG violations — decoupled from the HTTP request so
the punch endpoint returns fast and the heavy work runs asynchronously.

Note: ``recompute_daily_timesheet`` already runs ArbZG validation inline and
sets ``DailyTimesheet.status = FLAGGED`` when violations are found.  The
``check_arbzg`` task here surfaces those results back to the workflow context
so downstream tasks (e.g. notifications) can inspect them.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

from hatchet_sdk import Context
from pydantic import BaseModel

from zeiterfassung.core.hatchet import get_hatchet

hatchet = get_hatchet()


class PunchInput(BaseModel):
    tenant_id: str
    employee_id: str
    work_date: str  # ISO date string, e.g. "2026-03-25"


clock_processing = hatchet.workflow(name="clock-processing")


@clock_processing.task(execution_timeout=timedelta(seconds=30), retries=2)
async def recompute_timesheet(input: PunchInput, ctx: Context) -> dict:
    """Recompute the daily timesheet after a punch event."""
    from zeiterfassung.api.deps import _AsyncSessionLocal
    from zeiterfassung.services.clock_service import recompute_daily_timesheet

    tenant_id = uuid.UUID(input.tenant_id)
    employee_id = uuid.UUID(input.employee_id)
    work_date = date.fromisoformat(input.work_date)

    async with _AsyncSessionLocal() as db:
        result = await recompute_daily_timesheet(db, tenant_id, employee_id, work_date)
        await db.commit()
        return {
            "timesheet_id": str(result.id),
            "status": result.status.value,
            "flagged": result.status.value == "flagged",
            "flagged_reason": result.flagged_reason,
            "net_work_minutes": result.net_work_minutes,
            "gross_work_minutes": result.gross_work_minutes,
        }


@clock_processing.task(parents=[recompute_timesheet], execution_timeout=timedelta(seconds=10))
async def check_arbzg(input: PunchInput, ctx: Context) -> dict:
    """Surface ArbZG compliance results from the recomputed timesheet."""
    timesheet_data = ctx.task_output(recompute_timesheet)
    flagged = timesheet_data.get("flagged", False)
    flagged_reason = timesheet_data.get("flagged_reason")

    violations: list[str] = []
    if flagged and flagged_reason:
        violations = [v.strip() for v in flagged_reason.split(";") if v.strip()]

    return {
        "compliant": not flagged,
        "violations": violations,
    }
