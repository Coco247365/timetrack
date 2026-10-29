"""Public holidays API — exposes Feiertage for the configured Bundesland."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from zeiterfassung.api.deps import AuthUser, DBSession
from zeiterfassung.core.holidays import get_holidays
from zeiterfassung.services.tenant_service import resolve_bundesland

router = APIRouter()


@router.get("/holidays")
async def list_holidays(
    year: Annotated[int, Query(ge=2000, le=2100)],
    current_user: AuthUser,
    db: DBSession,
) -> dict[str, str]:
    """Return all public holidays for *year* in the tenant Bundesland.

    Response: mapping of ISO date string -> holiday name (German).
    Example: {"2025-01-01": "Neujahr", "2025-04-18": "Karfreitag", ...}
    """
    bundesland = await resolve_bundesland(db, current_user.tenant_id)
    holidays = get_holidays(year, bundesland)
    return {d.isoformat(): name for d, name in sorted(holidays.items())}
