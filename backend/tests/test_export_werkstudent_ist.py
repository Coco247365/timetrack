"""Werkstudent im LODAS-Export: genau eine Zeile Zeitlohn mit den Ist-Stunden.

Entscheidung 09.09.2026: Werkstudenten haben kein Soll und kein Konto; ihre
gearbeiteten Stunden (Tagesbogen-Netto) gehen als 011 in die Datei — keine
Staffel, keine Minusstunden, keine Abwesenheitszeilen.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest_asyncio

from zeiterfassung.core.security import hash_password
from zeiterfassung.models.clock import EventSource, EventType, PunchEvent
from zeiterfassung.models.user import User
from zeiterfassung.services import clock_service
from zeiterfassung.services.export_service import _build_datev_rows

WAGE_TYPES = {
    "NORMALE_ARBEITSZEIT": 11,
    "MEHRARBEIT_0": 62,
    "MEHRARBEIT_ZUSCHLAG": 66,
    "MINUSSTUNDEN": 13,
    "URLAUB_STD": 129,
}


def _utc(day: int, hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    local_minutes = h * 60 + m - 120  # Juni = CEST
    return datetime(2026, 6, day, local_minutes // 60, local_minutes % 60, tzinfo=UTC)


@pytest_asyncio.fixture
async def werkstudentin(db, tenant) -> User:
    now = datetime.now(tz=UTC)
    u = User(
        tenant_id=tenant.id,
        email="ws@example.com",
        hashed_password=hash_password("Passw0rd!"),
        first_name="Wanda",
        last_name="Werk",
        employee_number="22099",
        contracted_hours_per_week=20.0,
        employment_type="werkstudent",
        overtime_payout_mode="above_cap",
        overtime_cap_hours=0,
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    db.add(u)
    await db.flush()
    return u


class TestExportWerkstudent:
    async def test_nur_eine_zeile_zeitlohn_mit_ist(self, db, tenant, werkstudentin) -> None:
        tenant.settings = {
            **(tenant.settings or {}),
            "zuschlaege": {"export_basis": "soll", "mehrarbeit_frei_wochen_std": 1.5},
        }
        now = datetime.now(UTC)
        # Mo 01.06. 08:00–12:00 (4 h, keine Pause noetig) und Di 02.06.
        # 08:00–15:00 (7 h Anwesenheit -> 30 min ArbZG-Pause -> 6:30).
        for day, kommt, geht in ((1, "08:00", "12:00"), (2, "08:00", "15:00")):
            for etype, t in ((EventType.PUNCH_IN, kommt), (EventType.PUNCH_OUT, geht)):
                db.add(
                    PunchEvent(
                        tenant_id=tenant.id,
                        employee_id=werkstudentin.id,
                        event_type=etype,
                        event_time=_utc(day, t),
                        source=EventSource.NFC_TERMINAL,
                        created_at=now,
                    )
                )
        await db.flush()
        for d in (1, 2):
            await clock_service.recompute_daily_timesheet(
                db, tenant.id, werkstudentin.id, date(2026, 6, d)
            )
        await db.flush()

        rows = await _build_datev_rows(db, tenant.id, 2026, 6, [werkstudentin.id], WAGE_TYPES)
        assert [(r.lohnart, r.wert) for r in rows] == [(11, 10.5)]
