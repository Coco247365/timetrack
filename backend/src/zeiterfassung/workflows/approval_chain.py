"""Approval chain workflow.

Triggered when an absence request is submitted.  Notifies the appropriate
approver (Teamleiter or HR) about the pending request.

Push notifications are deferred to a future milestone (zeiterfassung-bfd).
For now the task logs the event and returns structured data for tracing.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from hatchet_sdk import Context
from pydantic import BaseModel

from zeiterfassung.core.hatchet import get_hatchet

logger = logging.getLogger(__name__)

hatchet = get_hatchet()


class ApprovalInput(BaseModel):
    tenant_id: str
    employee_id: str
    request_id: str
    request_type: str  # "vacation" | "sick_leave" | "other"
    date_from: str  # ISO date
    date_to: str  # ISO date


approval_chain = hatchet.workflow(name="approval-chain")


@approval_chain.task(execution_timeout=timedelta(seconds=15), retries=2)
async def notify_approver(input: ApprovalInput, ctx: Context) -> dict:
    """Send notification to Teamleiter/HR about a pending absence request.

    Push notification delivery is deferred — this task logs the event and
    will be extended once the push infrastructure is in place.
    """
    logger.info(
        "Absence request pending approval — tenant=%s employee=%s request=%s type=%s %s→%s",
        input.tenant_id,
        input.employee_id,
        input.request_id,
        input.request_type,
        input.date_from,
        input.date_to,
    )
    # TODO(zeiterfassung-bfd): resolve approver from department hierarchy and
    # deliver push/email notification via the notification service.
    return {
        "notified": True,
        "request_id": input.request_id,
        "channel": "log",  # will become "push" once integrated
    }
