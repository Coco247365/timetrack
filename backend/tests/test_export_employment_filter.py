"""DATEV-Export: Mitarbeiterkreis-Filter Lohn vs. Gehalt (#35).

Standard im Export-Dialog ist 'nur Lohn-Empfänger' (hourly) — Gehaltsempfänger
laufen fix über ihre Gehalts-Lohnart und sollen nicht als Stunden exportiert
werden. Der Filter gilt für Zeit-Zeilen UND manuelle Lohnbuchungen.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest_asyncio

from zeiterfassung.core.security import hash_password
from zeiterfassung.models.absence import RequestType
from zeiterfassung.models.user import User
from zeiterfassung.policy.context import DayPart
from zeiterfassung.schemas.absence import AbsenceRequestCreate
from zeiterfassung.services.absence_service import create_request
from zeiterfassung.services.export_service import _build_datev_rows

WAGE_TYPES = {"KRANK_STD": 153, "LA_0329": 329}


@pytest_asyncio.fixture
async def gehalt_employee(db, tenant) -> User:
    now = datetime.now(tz=UTC)
    u = User(
        tenant_id=tenant.id,
        email="gehalt@test.de",
        hashed_password=hash_password("Passw0rd!"),
        first_name="Gerda",
        last_name="Gehalt",
        employee_number="GEHALT1",
        contracted_hours_per_week=40.0,
        employment_type="salaried",
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    db.add(u)
    await db.flush()
    return u


async def _sick_day(db, tenant, employee, day: date):
    data = AbsenceRequestCreate.model_construct(
        request_type=RequestType.SICK_LEAVE,
        date_from=day,
        date_to=day,
        day_part=DayPart.FULL,
        hours=None,
        sick_status=None,
        payload=None,
        notes=None,
    )
    return await create_request(
        db, tenant.id, employee.id, data, "HH", auto_approve=True, approver_id=uuid.uuid4()
    )


class TestEmploymentFilter:
    async def _setup(self, db, tenant, employee, gehalt_employee) -> None:
        # employee (Fixture) ist hourly per Model-Default; beide je 1 Kranktag.
        await _sick_day(db, tenant, employee, date(2026, 6, 15))
        await _sick_day(db, tenant, gehalt_employee, date(2026, 6, 15))
        await db.flush()

    async def test_nur_lohn(self, db, tenant, employee, gehalt_employee) -> None:
        await self._setup(db, tenant, employee, gehalt_employee)
        rows = await _build_datev_rows(
            db, tenant.id, 2026, 6, None, WAGE_TYPES, employment_type="hourly"
        )
        pnrs = {r.personalnummer for r in rows}
        assert pnrs == {"EMP001"}  # nur der Stundenlohn-Mitarbeiter

    async def test_nur_gehalt(self, db, tenant, employee, gehalt_employee) -> None:
        await self._setup(db, tenant, employee, gehalt_employee)
        rows = await _build_datev_rows(
            db, tenant.id, 2026, 6, None, WAGE_TYPES, employment_type="salaried"
        )
        pnrs = {r.personalnummer for r in rows}
        assert pnrs == {"GEHALT1"}

    async def test_alle(self, db, tenant, employee, gehalt_employee) -> None:
        await self._setup(db, tenant, employee, gehalt_employee)
        rows = await _build_datev_rows(
            db, tenant.id, 2026, 6, None, WAGE_TYPES, employment_type=None
        )
        pnrs = {r.personalnummer for r in rows}
        assert pnrs == {"EMP001", "GEHALT1"}

    async def test_filter_gilt_auch_fuer_manuelle_buchungen(
        self, db, tenant, employee, gehalt_employee
    ) -> None:
        """Sonderzahlung des Gehaltsempfaengers darf bei 'nur Lohn' nicht mitkommen."""
        from zeiterfassung.models.payroll_booking import PayrollBooking

        # Lohnbuchungen sind ein Feature-Schalter (Default AUS) — fuer den
        # Filter-Test einschalten.
        tenant.settings = {**(tenant.settings or {}), "features": {"payroll_bookings": True}}
        await self._setup(db, tenant, employee, gehalt_employee)
        db.add(
            PayrollBooking(
                tenant_id=tenant.id,
                employee_id=gehalt_employee.id,
                year_month=date(2026, 6, 1),
                wage_type_code="LA_0329",
                value=200.0,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        await db.flush()
        rows = await _build_datev_rows(
            db, tenant.id, 2026, 6, None, WAGE_TYPES, employment_type="hourly"
        )
        assert {r.personalnummer for r in rows} == {"EMP001"}
        assert not any(r.lohnart == 329 for r in rows)
        # Ohne Filter kommt die Buchung mit.
        rows_all = await _build_datev_rows(
            db, tenant.id, 2026, 6, None, WAGE_TYPES, employment_type=None
        )
        assert any(r.lohnart == 329 and r.personalnummer == "GEHALT1" for r in rows_all)

    async def test_buchungen_nur_mit_feature_schalter(
        self, db, tenant, employee, gehalt_employee
    ) -> None:
        """Feature 'payroll_bookings' AUS (Default) -> manuelle Buchungen fehlen im Export."""
        from zeiterfassung.models.payroll_booking import PayrollBooking

        await self._setup(db, tenant, employee, gehalt_employee)
        db.add(
            PayrollBooking(
                tenant_id=tenant.id,
                employee_id=employee.id,
                year_month=date(2026, 6, 1),
                wage_type_code="LA_0329",
                value=150.0,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        await db.flush()
        # Feature aus (Default, keine features im Tenant): Buchung fehlt.
        rows_off = await _build_datev_rows(
            db, tenant.id, 2026, 6, None, WAGE_TYPES, employment_type=None
        )
        assert not any(r.lohnart == 329 for r in rows_off)
        # Feature an: Buchung ist drin.
        tenant.settings = {**(tenant.settings or {}), "features": {"payroll_bookings": True}}
        await db.flush()
        rows_on = await _build_datev_rows(
            db, tenant.id, 2026, 6, None, WAGE_TYPES, employment_type=None
        )
        assert any(r.lohnart == 329 and r.personalnummer == "EMP001" for r in rows_on)
