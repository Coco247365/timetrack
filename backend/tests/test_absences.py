"""Tests for absence service: leave request lifecycle, vacation balance."""

from __future__ import annotations

from datetime import UTC, date

import pytest
from pydantic import ValidationError

from zeiterfassung.models.absence import RequestStatus, RequestType
from zeiterfassung.schemas.absence import AbsenceRequestCreate
from zeiterfassung.services.absence_service import (
    ConflictError,
    InvalidStatusTransitionError,
    SelfApprovalError,
    approve_request,
    cancel_request,
    create_request,
    get_vacation_balance,
    reject_request,
)


class TestCreateRequest:
    async def test_create_vacation_request(self, db, tenant, employee) -> None:
        # 2027-07-05 (Mon) to 2027-07-09 (Fri) = 5 working days, no holidays
        data = AbsenceRequestCreate(
            request_type=RequestType.VACATION,
            date_from=date(2027, 7, 5),
            date_to=date(2027, 7, 9),
        )
        req = await create_request(db, tenant.id, employee.id, data, "HH")
        assert req.id is not None
        assert req.status == RequestStatus.SUBMITTED
        assert req.days_count == 5  # Mon-Fri, no holidays that week
        assert req.submitted_at is not None

    async def test_create_sick_leave(self, db, tenant, employee) -> None:
        # 2027-07-12 is a Monday (single day)
        data = AbsenceRequestCreate(
            request_type=RequestType.SICK_LEAVE,
            date_from=date(2027, 7, 12),
            date_to=date(2027, 7, 12),
        )
        req = await create_request(db, tenant.id, employee.id, data)
        assert req.request_type == RequestType.SICK_LEAVE
        assert req.days_count == 1

    async def test_invalid_date_range_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AbsenceRequestCreate(
                request_type=RequestType.VACATION,
                date_from=date(2027, 7, 18),
                date_to=date(2027, 7, 14),  # end before start
            )

    async def test_conflict_raises_error(self, db, tenant, employee) -> None:
        # 2027-08-02 (Mon) to 2027-08-06 (Fri)
        data = AbsenceRequestCreate(
            request_type=RequestType.VACATION,
            date_from=date(2027, 8, 2),
            date_to=date(2027, 8, 6),
        )
        await create_request(db, tenant.id, employee.id, data)
        # Overlapping request should raise ConflictError
        overlapping = AbsenceRequestCreate(
            request_type=RequestType.VACATION,
            date_from=date(2027, 8, 4),
            date_to=date(2027, 8, 10),
        )
        with pytest.raises(ConflictError):
            await create_request(db, tenant.id, employee.id, overlapping)


class TestApprovalWorkflow:
    async def test_approve_submitted_request(self, db, tenant, employee) -> None:
        import uuid

        approver_id = uuid.uuid4()
        # 2027-09-06 (Mon) to 2027-09-10 (Fri)
        data = AbsenceRequestCreate(
            request_type=RequestType.VACATION,
            date_from=date(2027, 9, 6),
            date_to=date(2027, 9, 10),
        )
        req = await create_request(db, tenant.id, employee.id, data)
        approved = await approve_request(db, tenant.id, req.id, approver_id, "Approved")
        assert approved.status == RequestStatus.APPROVED
        assert approved.decided_by == approver_id
        assert approved.decision_reason == "Approved"

    async def test_reject_submitted_request(self, db, tenant, employee) -> None:
        import uuid

        approver_id = uuid.uuid4()
        # 2027-09-13 (Mon) to 2027-09-17 (Fri)
        data = AbsenceRequestCreate(
            request_type=RequestType.VACATION,
            date_from=date(2027, 9, 13),
            date_to=date(2027, 9, 17),
        )
        req = await create_request(db, tenant.id, employee.id, data)
        rejected = await reject_request(db, tenant.id, req.id, approver_id, "Understaffed")
        assert rejected.status == RequestStatus.REJECTED
        assert rejected.decision_reason == "Understaffed"

    async def test_approve_already_approved_raises(self, db, tenant, employee) -> None:
        import uuid

        approver_id = uuid.uuid4()
        # 2027-10-04 (Mon) to 2027-10-08 (Fri)
        data = AbsenceRequestCreate(
            request_type=RequestType.VACATION,
            date_from=date(2027, 10, 4),
            date_to=date(2027, 10, 8),
        )
        req = await create_request(db, tenant.id, employee.id, data)
        await approve_request(db, tenant.id, req.id, approver_id)
        with pytest.raises(InvalidStatusTransitionError):
            await approve_request(db, tenant.id, req.id, approver_id)

    async def test_cancel_own_request(self, db, tenant, employee) -> None:
        # 2027-11-01 (Mon) to 2027-11-05 (Fri)
        data = AbsenceRequestCreate(
            request_type=RequestType.VACATION,
            date_from=date(2027, 11, 1),
            date_to=date(2027, 11, 5),
        )
        req = await create_request(db, tenant.id, employee.id, data)
        cancelled = await cancel_request(db, tenant.id, req.id, employee.id)
        assert cancelled.status == RequestStatus.CANCELLED

    async def test_cancel_approved_request_raises(self, db, tenant, employee) -> None:
        import uuid

        approver_id = uuid.uuid4()
        # 2027-11-08 (Mon) to 2027-11-12 (Fri)
        data = AbsenceRequestCreate(
            request_type=RequestType.VACATION,
            date_from=date(2027, 11, 8),
            date_to=date(2027, 11, 12),
        )
        req = await create_request(db, tenant.id, employee.id, data)
        await approve_request(db, tenant.id, req.id, approver_id)
        with pytest.raises(InvalidStatusTransitionError):
            await cancel_request(db, tenant.id, req.id, employee.id)

    async def test_self_approval_raises(self, db, tenant, employee) -> None:
        """Security: an approver must not approve their own leave request."""
        data = AbsenceRequestCreate(
            request_type=RequestType.VACATION,
            date_from=date(2027, 12, 6),
            date_to=date(2027, 12, 10),
        )
        req = await create_request(db, tenant.id, employee.id, data)
        with pytest.raises(SelfApprovalError):
            await approve_request(db, tenant.id, req.id, employee.id, "Self-approved")


class TestVacationBalance:
    async def test_initial_balance_is_full_entitlement(self, db, tenant, employee) -> None:
        balance = await get_vacation_balance(db, tenant.id, employee.id, 2027)
        assert balance.entitlement_days == 24.0
        assert balance.used_days == 0.0
        assert balance.pending_days == 0.0
        assert balance.remaining_days == 24.0

    async def test_approved_vacation_reduces_balance(self, db, tenant, employee) -> None:
        import uuid

        approver_id = uuid.uuid4()
        # 2027-07-19 (Mon) to 2027-07-23 (Fri) = 5 working days
        data = AbsenceRequestCreate(
            request_type=RequestType.VACATION,
            date_from=date(2027, 7, 19),
            date_to=date(2027, 7, 23),
        )
        req = await create_request(db, tenant.id, employee.id, data)
        await approve_request(db, tenant.id, req.id, approver_id)

        balance = await get_vacation_balance(db, tenant.id, employee.id, 2027)
        assert balance.used_days == 5.0
        # Neue Definition (2026-08-18): nur GENOMMENE Tage druecken den Rest —
        # der 2027er-Urlaub liegt in der Zukunft und ist "davon geplant".
        assert balance.planned_days == 5.0
        assert balance.remaining_days == 24.0

    async def test_pending_request_reduces_available(self, db, tenant, employee) -> None:
        # 2027-08-09 (Mon) to 2027-08-13 (Fri) = 5 working days
        data = AbsenceRequestCreate(
            request_type=RequestType.VACATION,
            date_from=date(2027, 8, 9),
            date_to=date(2027, 8, 13),
        )
        await create_request(db, tenant.id, employee.id, data)

        balance = await get_vacation_balance(db, tenant.id, employee.id, 2027)
        assert balance.pending_days == 5.0
        # Beantragt (pending) drueckt den Rest ebenfalls nicht mehr.
        assert balance.remaining_days == 24.0

    async def test_initial_remaining_is_base_in_hire_year(self, db, tenant, employee) -> None:
        """Eintrittsjahr: Resturlaub bildet die Basis (statt vollem Anspruch)."""
        employee.hire_date = date(2027, 7, 1)
        employee.vacation_entitlement_days = 30.0
        employee.initial_vacation_remaining_days = 12.0  # anteilig fürs Eintrittsjahr
        await db.flush()

        # Eintrittsjahr 2027: Basis 12, nichts genommen → 12 offen.
        bal2027 = await get_vacation_balance(db, tenant.id, employee.id, 2027)
        assert bal2027.entitlement_days == 12.0
        assert bal2027.remaining_days == 12.0

        # Nach Abzug: 3 Tage Urlaub → 9 offen.
        import uuid

        data = AbsenceRequestCreate(
            request_type=RequestType.VACATION,
            date_from=date(2027, 9, 6),
            date_to=date(2027, 9, 8),  # Mo–Mi = 3 Werktage
        )
        req = await create_request(db, tenant.id, employee.id, data, "HH")
        await approve_request(db, tenant.id, req.id, uuid.uuid4())
        bal2027b = await get_vacation_balance(db, tenant.id, employee.id, 2027)
        assert bal2027b.used_days == 3.0
        # Kuenftiger Urlaub = geplant, Rest bleibt die volle Basis.
        assert bal2027b.planned_days == 3.0
        assert bal2027b.remaining_days == 12.0

        # Folgejahr 2028: voller Jahresanspruch gilt wieder.
        bal2028 = await get_vacation_balance(db, tenant.id, employee.id, 2028)
        assert bal2028.entitlement_days == 30.0
        assert bal2028.remaining_days == 30.0

    async def test_initial_remaining_uses_counting_start_year(self, db, tenant, employee) -> None:
        """Erfassungsbeginn schlaegt Eintrittsjahr: der uebernommene Reststand
        gilt im Jahr des Systemstarts (Go-Live 07/2026), nicht im — womoeglich
        Jahrzehnte zurueckliegenden — Eintrittsjahr."""
        employee.hire_date = date(2015, 3, 1)  # lange vor dem Systemstart
        employee.counting_start_date = date(2026, 7, 1)
        employee.vacation_entitlement_days = 30.0
        employee.initial_vacation_remaining_days = 26.0  # Reststand aus der Lohnabrechnung
        await db.flush()

        # Startjahr 2026: uebernommener Reststand ist die Basis.
        bal2026 = await get_vacation_balance(db, tenant.id, employee.id, 2026)
        assert bal2026.entitlement_days == 26.0
        # Folgejahr: wieder voller Jahresanspruch.
        bal2027 = await get_vacation_balance(db, tenant.id, employee.id, 2027)
        assert bal2027.entitlement_days == 30.0

    async def test_initial_remaining_ignored_without_hire_date(self, db, tenant, employee) -> None:
        """Ohne hire_date greift der Resturlaub-Sonderwert nicht (Fallback Anspruch)."""
        employee.hire_date = None
        employee.vacation_entitlement_days = 30.0
        employee.initial_vacation_remaining_days = 12.0
        await db.flush()
        bal = await get_vacation_balance(db, tenant.id, employee.id, 2027)
        assert bal.entitlement_days == 30.0

    async def test_worked_vacation_day_is_refunded(self, db, tenant, employee) -> None:
        """#22: An einem genehmigten Urlaubstag gearbeitet → Urlaub kommt zurück."""
        import uuid
        from datetime import datetime

        from zeiterfassung.models.clock import DailyTimesheet
        from zeiterfassung.policy.context import DayPart

        # Vergangener Urlaub Mo–Fr (5 Tage), genehmigt.
        data = AbsenceRequestCreate.model_construct(
            request_type=RequestType.VACATION,
            date_from=date(2026, 6, 15),
            date_to=date(2026, 6, 19),
            day_part=DayPart.FULL,
            hours=None,
            payload=None,
            notes=None,
        )
        await create_request(
            db, tenant.id, employee.id, data, "HH", auto_approve=True, approver_id=uuid.uuid4()
        )
        # An zwei dieser Tage wurde tatsächlich gearbeitet (Tagesbogen vorhanden).
        now = datetime(2026, 6, 15, 8, 0, tzinfo=UTC)
        for d in (date(2026, 6, 16), date(2026, 6, 17)):
            db.add(
                DailyTimesheet(
                    tenant_id=tenant.id,
                    employee_id=employee.id,
                    work_date=d,
                    start_time=now,
                    break_minutes=0,
                    net_work_minutes=480,
                    gross_work_minutes=480,
                    overtime_minutes=0,
                    is_holiday_work=False,
                    created_at=now,
                    updated_at=now,
                )
            )
        await db.flush()

        balance = await get_vacation_balance(db, tenant.id, employee.id, 2026)
        # 5 genommen − 2 gearbeitet = 3 verbraucht; Rest 24 − 3 = 21.
        assert balance.used_days == 3.0
        assert balance.remaining_days == 21.0
        assert balance.past_days == 3.0  # gearbeitete Tage auch aus „genommen" raus


class TestZeitraumAendern:
    async def test_genehmigte_krankmeldung_verkuerzen(self, db, tenant, employee):
        from datetime import date

        from zeiterfassung.models.absence import RequestStatus, RequestType
        from zeiterfassung.policy.context import DayPart
        from zeiterfassung.schemas.absence import AbsenceRequestCreate
        from zeiterfassung.services.absence_service import create_request, update_request_period

        # Rückwirkende Krankmeldung (wie Admin/Mail): Self-Service-Vergangenheits-Guard
        # via model_construct umgehen — sonst bricht der Test, sobald das Datum vorbei ist.
        req = await create_request(
            db,
            tenant.id,
            employee.id,
            AbsenceRequestCreate.model_construct(
                request_type=RequestType.SICK_LEAVE,
                date_from=date(2026, 7, 6),
                date_to=date(2026, 7, 8),
                day_part=DayPart.FULL,
                hours=None,
                payload=None,
                notes=None,
            ),
            auto_approve=True,
        )
        assert req.status == RequestStatus.APPROVED
        req = await update_request_period(
            db, tenant.id, req.id, date(2026, 7, 6), date(2026, 7, 7), None
        )
        assert (req.date_from, req.date_to) == (date(2026, 7, 6), date(2026, 7, 7))
        assert float(req.days_count) == 2.0
        assert req.status == RequestStatus.APPROVED  # Status bleibt


class TestTeilTagStunden:
    """Issue #7: Stunden-Teil-Tag gilt nur für EINEN Tag — serverseitig erzwungen.

    Vorher multiplizierte frac = hours/daily jeden Tag des Bereichs
    (hours=4 über Mo–Fr → 2,5 Tage statt eines Halbtags).
    """

    async def test_schema_rejects_hours_with_date_range(self) -> None:
        with pytest.raises(ValidationError):
            AbsenceRequestCreate(
                request_type=RequestType.VACATION,
                date_from=date(2027, 7, 5),
                date_to=date(2027, 7, 9),
                hours=4.0,
            )

    async def test_schema_rejects_hours_out_of_bounds(self) -> None:
        for bad in (0.0, -1.0, 25.0):
            with pytest.raises(ValidationError):
                AbsenceRequestCreate(
                    request_type=RequestType.VACATION,
                    date_from=date(2027, 7, 5),
                    date_to=date(2027, 7, 5),
                    hours=bad,
                )

    async def test_service_rejects_hours_with_range_despite_construct(
        self, db, tenant, employee
    ) -> None:
        # Der Admin-Pfad umgeht Schema-Validatoren via model_construct —
        # der Service muss die Regel selbst durchsetzen.
        from zeiterfassung.policy.context import DayPart

        data = AbsenceRequestCreate.model_construct(
            request_type=RequestType.VACATION,
            date_from=date(2027, 7, 5),
            date_to=date(2027, 7, 9),
            day_part=DayPart.FULL,
            hours=4.0,
            payload=None,
            notes=None,
        )
        with pytest.raises(ConflictError):
            await create_request(db, tenant.id, employee.id, data, "HH")

    async def test_single_day_hours_gives_half_day(self, db, tenant, employee) -> None:
        # 40 h/Woche → 8 h Tagessoll; 4 h = 0,5 Tage.
        data = AbsenceRequestCreate(
            request_type=RequestType.VACATION,
            date_from=date(2027, 7, 5),
            date_to=date(2027, 7, 5),
            hours=4.0,
        )
        req = await create_request(db, tenant.id, employee.id, data, "HH")
        assert float(req.days_count) == 0.5

    async def test_period_update_keeps_part_day_fraction(self, db, tenant, employee) -> None:
        from zeiterfassung.services.absence_service import update_request_period

        data = AbsenceRequestCreate(
            request_type=RequestType.VACATION,
            date_from=date(2027, 7, 5),
            date_to=date(2027, 7, 5),
            hours=4.0,
        )
        req = await create_request(db, tenant.id, employee.id, data, "HH")
        # Auf einen anderen Einzeltag verschieben: 0,5 bleibt 0,5 (vorher: still → 1,0).
        req = await update_request_period(
            db, tenant.id, req.id, date(2027, 7, 6), date(2027, 7, 6), None
        )
        assert float(req.days_count) == 0.5

    async def test_period_update_cannot_expand_part_day_to_range(
        self, db, tenant, employee
    ) -> None:
        from zeiterfassung.services.absence_service import update_request_period

        data = AbsenceRequestCreate(
            request_type=RequestType.VACATION,
            date_from=date(2027, 7, 5),
            date_to=date(2027, 7, 5),
            hours=4.0,
        )
        req = await create_request(db, tenant.id, employee.id, data, "HH")
        with pytest.raises(ConflictError):
            await update_request_period(
                db, tenant.id, req.id, date(2027, 7, 5), date(2027, 7, 9), None
            )
