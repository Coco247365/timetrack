"""AU/LFZ-Status an Krankmeldungen (#24)."""

from __future__ import annotations

import uuid
from datetime import date

import pytest

from zeiterfassung.models.absence import RequestType, SickStatus
from zeiterfassung.policy.context import DayPart
from zeiterfassung.schemas.absence import AbsenceRequestCreate
from zeiterfassung.services.absence_service import (
    ConflictError,
    create_request,
    get_request,
    set_sick_status,
)


async def _sick(db, tenant, employee, sick_status=None):
    data = AbsenceRequestCreate.model_construct(
        request_type=RequestType.SICK_LEAVE,
        date_from=date(2026, 6, 15),
        date_to=date(2026, 6, 15),
        day_part=DayPart.FULL,
        hours=None,
        sick_status=sick_status,
        payload=None,
        notes=None,
    )
    return await create_request(
        db, tenant.id, employee.id, data, "HH", auto_approve=True, approver_id=uuid.uuid4()
    )


class TestSickStatus:
    async def test_create_with_status(self, db, tenant, employee) -> None:
        req = await _sick(db, tenant, employee, SickStatus.AU_MIT_LFZ)
        assert req.sick_status == SickStatus.AU_MIT_LFZ

    async def test_change_status_afterwards(self, db, tenant, employee) -> None:
        req = await _sick(db, tenant, employee, None)
        assert req.sick_status is None
        updated = await set_sick_status(
            db, tenant.id, req.id, SickStatus.EAU_TELEFON, actor_id=None
        )
        assert updated.sick_status == SickStatus.EAU_TELEFON
        # Persistiert?
        again = await get_request(db, tenant.id, req.id)
        assert again.sick_status == SickStatus.EAU_TELEFON

    async def test_clear_status(self, db, tenant, employee) -> None:
        req = await _sick(db, tenant, employee, SickStatus.OHNE_AU_OHNE_LFZ)
        updated = await set_sick_status(db, tenant.id, req.id, None, actor_id=None)
        assert updated.sick_status is None

    async def test_status_only_for_sick_leave(self, db, tenant, employee) -> None:
        # Urlaub bekommt keinen AU-Status, auch wenn übergeben.
        data = AbsenceRequestCreate(
            request_type=RequestType.VACATION,
            date_from=date(2027, 7, 5),
            date_to=date(2027, 7, 5),
            sick_status=SickStatus.AU_MIT_LFZ,
        )
        req = await create_request(db, tenant.id, employee.id, data, "HH")
        assert req.sick_status is None
        # set_sick_status auf Urlaub → Konflikt.
        with pytest.raises(ConflictError):
            await set_sick_status(db, tenant.id, req.id, SickStatus.AU_MIT_LFZ, actor_id=None)
