"""Daily anomaly check workflow.

Cron workflow that runs at 06:00 UTC each day to scan yesterday's timesheets
for anomalies:
  - Missing clock-out (clock-in recorded but no clock-out)
  - Missing break for shifts > 6 h (ArbZG §4)
  - Incomplete days (no events at all for contracted employees)

Results are grouped by department and Teamleiters are notified.
Notification delivery is deferred — grouped results are logged until the
push infrastructure is in place.
"""

from __future__ import annotations

import logging
from datetime import UTC, timedelta
from datetime import datetime as dt

from hatchet_sdk import Context
from pydantic import BaseModel

from zeiterfassung.core.hatchet import get_hatchet

logger = logging.getLogger(__name__)

hatchet = get_hatchet()

daily_check = hatchet.workflow(name="daily-anomaly-check", on_crons=["0 6 * * *"])


class _EmptyInput(BaseModel):
    """Cron workflows receive no user input."""


@daily_check.task(execution_timeout=timedelta(seconds=60))
async def find_anomalies(input: _EmptyInput, ctx: Context) -> dict:
    """Scan yesterday's timesheets for anomalies across all tenants."""
    from sqlalchemy import select

    from zeiterfassung.api.deps import _AsyncSessionLocal
    from zeiterfassung.models.clock import DailyTimesheet
    from zeiterfassung.services.clock_service import detect_anomalies

    yesterday = (dt.now(tz=UTC) - timedelta(days=1)).date()

    results: list[dict] = []

    async with _AsyncSessionLocal() as db:
        stmt = select(DailyTimesheet).where(DailyTimesheet.work_date == yesterday)
        rows = list((await db.execute(stmt)).scalars().all())

    for ts in rows:
        issues = detect_anomalies(ts)
        if issues:
            results.append(
                {
                    "tenant_id": str(ts.tenant_id),
                    "employee_id": str(ts.employee_id),
                    "work_date": str(ts.work_date),
                    "anomalies": issues,
                }
            )

    logger.info(
        "Daily anomaly check for %s — %d timesheets scanned, %d with anomalies",
        yesterday,
        len(rows),
        len(results),
    )
    return {"date": str(yesterday), "anomalies": results}


@daily_check.task(parents=[find_anomalies])
async def notify_teamleiters(input: _EmptyInput, ctx: Context) -> dict:
    """Notify each Teamleiter about their team's anomalies.

    Groups anomalies by tenant/department and logs them.  Will dispatch push
    notifications once the notification service is integrated.
    """
    data = ctx.task_output(find_anomalies)
    anomalies: list[dict] = data.get("anomalies", [])

    if not anomalies:
        return {"notifications_sent": 0}

    # Group by tenant for structured logging — department grouping will be added
    # when the notification service is available.
    by_tenant: dict[str, list[dict]] = {}
    for item in anomalies:
        tid = item["tenant_id"]
        by_tenant.setdefault(tid, []).append(item)

    for tenant_id, items in by_tenant.items():
        logger.warning(
            "Anomaly summary for tenant=%s date=%s — %d employees affected",
            tenant_id,
            data.get("date"),
            len(items),
        )
        for item in items:
            for anomaly in item["anomalies"]:
                logger.warning("  employee=%s: %s", item["employee_id"], anomaly)

    # TODO(zeiterfassung-bfd): resolve department → Teamleiter mapping and
    # dispatch per-department push/email notifications.
    return {"notifications_sent": 0}
