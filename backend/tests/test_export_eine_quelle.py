"""Export liest das Ist aus dem Tagesbogen — eine Quelle mit Zettel und Konto.

ein Mitarbeiter 08/2026 zeigte +10:15 h mehr Mehrarbeit im Export als auf dem Zettel:
der Export rechnete aus den Roh-Segmenten (ohne die automatisch angehobene
ArbZG-Mindestpause), Zettel/Konto aus dem Tagesbogen. Jetzt gilt der Bogen.
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
}


def _utc(day: int, hhmm: str) -> datetime:
    """Lokale Berliner Zeit (Juni = CEST, UTC+2) -> UTC."""
    h, m = map(int, hhmm.split(":"))
    local_minutes = h * 60 + m - 120
    return datetime(2026, 6, day, local_minutes // 60, local_minutes % 60, tzinfo=UTC)


@pytest_asyncio.fixture
async def staffel_employee(db, tenant) -> User:
    now = datetime.now(tz=UTC)
    u = User(
        tenant_id=tenant.id,
        email="staffel@example.com",
        hashed_password=hash_password("Passw0rd!"),
        first_name="Sina",
        last_name="Staffel",
        employee_number="21099",
        contracted_hours_per_week=38.5,
        weekday_target_minutes=[495, 495, 495, 495, 330, 0, 0],
        employment_type="hourly",
        overtime_payout_mode="all",
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    db.add(u)
    await db.flush()
    return u


async def _punch(db, tenant, emp, day: int, kommt: str, geht: str) -> None:
    now = datetime.now(UTC)
    for etype, t in ((EventType.PUNCH_IN, kommt), (EventType.PUNCH_OUT, geht)):
        db.add(
            PunchEvent(
                tenant_id=tenant.id,
                employee_id=emp.id,
                event_type=etype,
                event_time=_utc(day, t),
                source=EventSource.NFC_TERMINAL,
                created_at=now,
            )
        )


class TestExportEineQuelle:
    async def test_mindestpause_zaehlt_im_export_wie_auf_dem_zettel(
        self, db, tenant, staffel_employee
    ) -> None:
        tenant.settings = {
            **(tenant.settings or {}),
            "zuschlaege": {"export_basis": "soll", "mehrarbeit_frei_wochen_std": 1.5},
        }
        # Mo 01.06.: 06:00–14:15 ohne gestempelte Pause. Roh = 495 = Soll;
        # der Tagesbogen setzt 30 min ArbZG-Pause an -> Ist 465 -> −30 min.
        # Di 02.06.: 06:00–16:45 ohne Pause. Roh = 645; Bogen: 45 min Pause
        # (>9 h) -> 600 -> +105 min (90 frei, 15 mit Zuschlag).
        await _punch(db, tenant, staffel_employee, 1, "06:00", "14:15")
        await _punch(db, tenant, staffel_employee, 2, "06:00", "16:45")
        await db.flush()
        for d in (1, 2):
            await clock_service.recompute_daily_timesheet(
                db, tenant.id, staffel_employee.id, date(2026, 6, d)
            )
        await db.flush()

        rows = await _build_datev_rows(db, tenant.id, 2026, 6, [staffel_employee.id], WAGE_TYPES)
        by_la = {r.lohnart: r.wert for r in rows}
        # Minusstunden: die 20 nicht gestempelten Werktage (Juni-Soll 170:30
        # minus 2 x 8:15 = 154:00) plus die 30 min Mindestpause vom Montag.
        # Mit den Roh-Segmenten staende hier 013=154.00, 062=1.50, 066=1.00.
        assert by_la.get(11) == 170.5, by_la
        assert by_la.get(13) == 154.5, by_la
        assert by_la.get(62) == 1.5, by_la
        assert by_la.get(66) == 0.25, by_la
