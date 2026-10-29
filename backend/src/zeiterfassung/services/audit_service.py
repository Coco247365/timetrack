"""Helper for writing AuditLog entries.

The ``log`` helper only adds the row to the current session (``db.add``); the
caller is responsible for committing it as part of its own transaction.  This
keeps the audit entry atomic with the action it records.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from zeiterfassung.models.audit import AuditLog


async def log(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    action: str,
    resource_type: str | None = None,
    resource_id: str | None = None,
    ip_address: str | None = None,
    details: dict | None = None,
) -> None:
    """Add an :class:`AuditLog` row to *db* (no commit).

    The entry is committed by the caller as part of the surrounding
    transaction so the audit trail stays consistent with the recorded action.
    """
    entry = AuditLog(
        tenant_id=tenant_id,
        actor_id=actor_id,
        action=action,
        resource_type=resource_type,
        resource_id=str(resource_id) if resource_id is not None else None,
        ip_address=ip_address,
        details=details,
        created_at=datetime.now(tz=UTC),
    )
    db.add(entry)
