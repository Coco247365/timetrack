"""Tenant-level helpers shared across services."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from zeiterfassung.config import get_settings
from zeiterfassung.models.tenant import Tenant

_DEFAULT_TIMEZONE = "Europe/Berlin"


def tenant_timezone(tenant: Tenant | None) -> ZoneInfo:
    """Resolve a tenant's IANA timezone, defaulting to ``Europe/Berlin``.

    Falls back safely if ``tenant`` is ``None`` or holds an invalid/unknown
    zone name — clock/timesheet bucketing must never crash on a bad value.
    """
    try:
        return ZoneInfo(tenant.timezone if tenant and tenant.timezone else _DEFAULT_TIMEZONE)
    except Exception:
        return ZoneInfo(_DEFAULT_TIMEZONE)


def tenant_local_datetime(dt: datetime, tenant: Tenant | None) -> datetime:
    """Convert a tz-aware datetime (normally stored as UTC) to the tenant's local time."""
    return dt.astimezone(tenant_timezone(tenant))


def tenant_local_date(dt: datetime, tenant: Tenant | None) -> date:
    """Return the tenant-local calendar date for a tz-aware UTC datetime.

    Used to bucket punch events into the correct work day: a punch at 23:20 UTC
    (00:20 CET for a Europe/Berlin tenant) must land on the NEXT calendar day,
    not on the UTC day. Storage stays UTC — only date/hour *derivations* route
    through this helper (see #3).
    """
    return tenant_local_datetime(dt, tenant).date()


async def get_tenant_or_404(db: AsyncSession, tenant_id: uuid.UUID) -> Tenant:
    """Load a tenant or raise the standard 404 used across admin config endpoints.

    Moved verbatim (as a shared helper) out of the several ``api/v1/admin.py``
    handlers that repeated ``select(Tenant)... or 404`` (zeiterfassung-6pcv).
    """
    from fastapi import HTTPException, status

    tenant = (await db.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one_or_none()
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    return tenant


async def resolve_bundesland(db: AsyncSession, tenant_id: uuid.UUID) -> str:
    """Return the Bundesland (ISO 3166-2:DE) for holiday/absence calculations.

    Reads the per-tenant ``Tenant.bundesland`` column so every tenant gets its
    own public holidays. Falls back to the global default only if the tenant
    row is missing (should not happen in practice).

    A per-employee override is intentionally deferred (bead q1a follow-up):
    once ``User.bundesland`` exists the precedence becomes
    ``user.bundesland or tenant.bundesland or default``.
    """
    bundesland = (
        await db.execute(select(Tenant.bundesland).where(Tenant.id == tenant_id))
    ).scalar_one_or_none()
    return bundesland or get_settings().default_bundesland
