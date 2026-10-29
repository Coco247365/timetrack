"""q1a: Feiertage werden je Mandant aus ``Tenant.bundesland`` aufgelöst.

Vorher las die Feiertags-/Abwesenheits-Rechnung global ``settings.default_bundesland``
("HH"), sodass jeder Mandant dieselben Feiertage bekam. Diese Tests sichern, dass
``resolve_bundesland`` die pro-Mandant-Spalte liest und ein nicht-HH-Mandant andere
Feiertage erhält.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from zeiterfassung.config import get_settings
from zeiterfassung.core.holidays import get_holidays
from zeiterfassung.models.tenant import Tenant
from zeiterfassung.services.tenant_service import resolve_bundesland


async def test_resolve_bundesland_reads_tenant_column(db: AsyncSession) -> None:
    t = Tenant(name="Demo KG", bundesland="BY")
    db.add(t)
    await db.flush()
    assert await resolve_bundesland(db, t.id) == "BY"


async def test_resolve_bundesland_falls_back_when_tenant_missing(db: AsyncSession) -> None:
    assert await resolve_bundesland(db, uuid.uuid4()) == get_settings().default_bundesland


def test_bavarian_holiday_differs_from_hamburg() -> None:
    # Mariä Himmelfahrt (15. Aug) ist Feiertag in BY, nicht in HH.
    assert date(2025, 8, 15) in get_holidays(2025, "BY")
    assert date(2025, 8, 15) not in get_holidays(2025, "HH")
