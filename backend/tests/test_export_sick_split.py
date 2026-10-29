"""DATEV-Export: Kranktage nach AU-Status aufgeschlüsselt (#28).

Kranktage werden pro AU-Status (sick_status) auf eigene Export-Kategorien
gebucht, sofern die Kategorie eine Lohnart zugeordnet hat. Ohne Status oder
ohne Zuordnung fallen die Tage auf die Sammel-Kategorie KRANK_STD zurück —
das bisherige Verhalten bleibt also ohne Konfiguration unverändert.
"""

from __future__ import annotations

import uuid
from datetime import date

from zeiterfassung.models.absence import RequestType, SickStatus
from zeiterfassung.payroll.categories import all_categories
from zeiterfassung.policy.context import DayPart
from zeiterfassung.schemas.absence import AbsenceRequestCreate
from zeiterfassung.services.absence_service import create_request
from zeiterfassung.services.export_service import (
    SICK_STATUS_CATEGORY,
    _build_datev_rows,
)


async def _sick_day(db, tenant, employee, day: date, sick_status=None):
    data = AbsenceRequestCreate.model_construct(
        request_type=RequestType.SICK_LEAVE,
        date_from=day,
        date_to=day,
        day_part=DayPart.FULL,
        hours=None,
        sick_status=sick_status,
        payload=None,
        notes=None,
    )
    return await create_request(
        db, tenant.id, employee.id, data, "HH", auto_approve=True, approver_id=uuid.uuid4()
    )


def test_jeder_sick_status_hat_eine_kategorie():
    """Jeder Enum-Wert ist gemappt und jede gemappte Kategorie existiert."""
    codes = {c.code for c in all_categories()}
    for status in SickStatus:
        assert str(status) in SICK_STATUS_CATEGORY
        assert SICK_STATUS_CATEGORY[str(status)] in codes


class TestExportKrankSplit:
    async def test_split_nach_au_status_mit_fallback(self, db, tenant, employee) -> None:
        # 4 einzelne Kranktage im Juni 2026 (Mo 15. – Do 18., keine Feiertage in HH):
        await _sick_day(db, tenant, employee, date(2026, 6, 15), SickStatus.AU_MIT_LFZ)
        await _sick_day(db, tenant, employee, date(2026, 6, 16), SickStatus.OHNE_AU_MIT_LFZ)
        await _sick_day(db, tenant, employee, date(2026, 6, 17), None)  # kein Status
        # EAU_TELEFON hat unten KEINE Zuordnung -> muss auf KRANK_STD zurückfallen.
        await _sick_day(db, tenant, employee, date(2026, 6, 18), SickStatus.EAU_TELEFON)
        await db.flush()

        wage_types = {
            "KRANK_STD": 150,
            "KRANK_MIT_AU": 153,
            "KRANK_OHNE_AU_MIT_LFZ": 154,
            # KRANK_EAU_TELEFON bewusst nicht zugeordnet.
        }
        rows = await _build_datev_rows(db, tenant.id, 2026, 6, [employee.id], wage_types)
        by_lohnart = {r.lohnart: r.wert for r in rows}

        # 40h-Woche -> 8h Tagessoll je Krank-Tag.
        assert by_lohnart[153] == 8.0  # mit AU
        assert by_lohnart[154] == 8.0  # ohne AU, mit LFZ
        # Fallback: Tag ohne Status + EAU-Tag ohne Zuordnung = 2 Tage auf KRANK_STD.
        assert by_lohnart[150] == 16.0

    async def test_ohne_konfiguration_alles_auf_krank_std(self, db, tenant, employee) -> None:
        """Bisheriges Verhalten: nur KRANK_STD zugeordnet -> alles landet dort."""
        await _sick_day(db, tenant, employee, date(2026, 6, 15), SickStatus.AU_MIT_LFZ)
        await _sick_day(db, tenant, employee, date(2026, 6, 16), None)
        await db.flush()

        rows = await _build_datev_rows(db, tenant.id, 2026, 6, [employee.id], {"KRANK_STD": 153})
        by_lohnart = {r.lohnart: r.wert for r in rows}
        assert by_lohnart == {153: 16.0}
