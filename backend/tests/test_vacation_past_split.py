"""Urlaubsblock über heute hinaus: vergangene Tage zählen als GENOMMEN.

Vorher galt ein Block erst als genommen, wenn er komplett vorbei war
(``date_to < today``). Bei Zaharia (12.08.–18.09., heute 17.08.) standen
dadurch auch der 12.–14.08. als "geplant", obwohl sie längst gelebt sind.

Der heutige Tag zählt bewusst weiter als geplant.
"""

from __future__ import annotations

from datetime import date

import pytest

from zeiterfassung.models.absence import RequestType
from zeiterfassung.policy.context import DayPart
from zeiterfassung.schemas.absence import AbsenceRequestCreate
from zeiterfassung.services.absence_service import (
    approve_request,
    create_request,
    get_vacation_balance,
)

pytestmark = pytest.mark.asyncio

# Mi 12.08. bis Fr 18.09.2026; "heute" ist Mo 17.08.
BLOCK_VON = date(2026, 8, 12)
BLOCK_BIS = date(2026, 9, 18)
HEUTE = date(2026, 8, 17)


async def _block(db, tenant, employee, von=BLOCK_VON, bis=BLOCK_BIS):
    req = await create_request(
        db,
        tenant.id,
        employee.id,
        AbsenceRequestCreate.model_construct(
            request_type=RequestType.VACATION, date_from=von, date_to=bis,
            day_part=DayPart.FULL, hours=None, sick_status=None, notes=None, payload=None,
        ),
    )
    await approve_request(db, tenant.id, req.id, None)
    return req


class TestBlockUeberHeute:
    async def test_vergangene_tage_zaehlen_als_genommen(self, db, tenant, employee):
        req = await _block(db, tenant, employee)
        b = await get_vacation_balance(db, tenant.id, employee.id, 2026, today=HEUTE)
        # 12., 13., 14.08. sind vorbei (15./16. = Wochenende) -> 3 Tage genommen.
        assert b.past_days == pytest.approx(3.0)
        assert b.planned_days == pytest.approx(float(req.days_count) - 3.0)

    async def test_heute_zaehlt_noch_als_geplant(self, db, tenant, employee):
        """Der 17.08. selbst darf NICHT in 'genommen' rutschen."""
        await _block(db, tenant, employee)
        heute = await get_vacation_balance(db, tenant.id, employee.id, 2026, today=HEUTE)
        morgen = await get_vacation_balance(
            db, tenant.id, employee.id, 2026, today=date(2026, 8, 18)
        )
        assert morgen.past_days == pytest.approx(heute.past_days + 1.0)

    async def test_summe_bleibt_unveraendert(self, db, tenant, employee):
        """Die Aufteilung verschiebt nur — Verbrauch und Rest bleiben gleich."""
        req = await _block(db, tenant, employee)
        b = await get_vacation_balance(db, tenant.id, employee.id, 2026, today=HEUTE)
        assert b.past_days + b.planned_days == pytest.approx(float(req.days_count))
        assert b.used_days == pytest.approx(float(req.days_count))
        # Rest wird bei null gedeckelt (der Block ist laenger als der Anspruch
        # der Test-Fixture) — die Aufteilung darf daran nichts aendern.
        # Neue Definition (2026-08-18): nur der GENOMMENE Anteil des Blocks
        # drueckt den Rest, der geplante Teil ist "davon geplant".
        erwartet = max(0.0, b.entitlement_days - b.past_days)
        assert b.remaining_days == pytest.approx(erwartet)


class TestUnveraendertesVerhalten:
    async def test_block_komplett_vorbei_ist_ganz_genommen(self, db, tenant, employee):
        req = await _block(db, tenant, employee, date(2026, 3, 2), date(2026, 3, 6))
        b = await get_vacation_balance(db, tenant.id, employee.id, 2026, today=HEUTE)
        assert b.past_days == pytest.approx(float(req.days_count))
        assert b.planned_days == pytest.approx(0.0)

    async def test_block_komplett_kuenftig_ist_ganz_geplant(self, db, tenant, employee):
        req = await _block(db, tenant, employee, date(2026, 10, 5), date(2026, 10, 9))
        b = await get_vacation_balance(db, tenant.id, employee.id, 2026, today=HEUTE)
        assert b.past_days == pytest.approx(0.0)
        assert b.planned_days == pytest.approx(float(req.days_count))

    async def test_block_beginnt_heute_ist_ganz_geplant(self, db, tenant, employee):
        req = await _block(db, tenant, employee, HEUTE, date(2026, 8, 21))
        b = await get_vacation_balance(db, tenant.id, employee.id, 2026, today=HEUTE)
        assert b.past_days == pytest.approx(0.0)
        assert b.planned_days == pytest.approx(float(req.days_count))
