"""Tests for the approval-context endpoint logic.

Since the endpoint is assembled from service calls and direct queries, we test
the integration at the service/query level using the in-memory SQLite DB.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from zeiterfassung.models.absence import ApprovalRequest, RequestStatus, RequestType
from zeiterfassung.models.tenant import Tenant
from zeiterfassung.models.user import User
from zeiterfassung.schemas.absence import AbsenceRequestCreate
from zeiterfassung.services.absence_service import (
    approve_request,
    create_request,
    get_vacation_balance,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_employee(db: AsyncSession, tenant: Tenant, first: str, last: str) -> User:
    from zeiterfassung.core.security import hash_password

    now = datetime.now(tz=UTC)
    u = User(
        tenant_id=tenant.id,
        email=f"{first.lower()}.{last.lower()}@test.de",
        hashed_password=hash_password("Passw0rd!"),
        first_name=first,
        last_name=last,
        employee_number=f"EMP-{first[:3].upper()}",
        contracted_hours_per_week=40.0,
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    db.add(u)
    await db.flush()
    return u


async def _get_overlapping(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    request_id: uuid.UUID,
    date_from: date,
    date_to: date,
) -> list[ApprovalRequest]:
    """Replicate the overlap query from the endpoint."""
    result = await db.execute(
        select(ApprovalRequest).where(
            ApprovalRequest.tenant_id == tenant_id,
            ApprovalRequest.id != request_id,
            ApprovalRequest.date_from <= date_to,
            ApprovalRequest.date_to >= date_from,
            ApprovalRequest.status.in_([RequestStatus.APPROVED, RequestStatus.SUBMITTED]),
        )
    )
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestApprovalContextLogic:
    async def test_no_overlap_when_no_other_requests(
        self, db: AsyncSession, tenant: Tenant, employee: User
    ) -> None:
        data = AbsenceRequestCreate(
            request_type=RequestType.VACATION,
            date_from=date(2027, 7, 5),
            date_to=date(2027, 7, 9),
        )
        req = await create_request(db, tenant.id, employee.id, data)

        overlapping = await _get_overlapping(db, tenant.id, req.id, req.date_from, req.date_to)
        assert overlapping == []

    async def test_approved_colleague_appears_in_overlap(
        self, db: AsyncSession, tenant: Tenant, employee: User
    ) -> None:
        colleague = await _make_employee(db, tenant, "Anna", "Schmidt")
        approver_id = uuid.uuid4()

        # Colleague has approved vacation overlapping the same week
        colleague_data = AbsenceRequestCreate(
            request_type=RequestType.VACATION,
            date_from=date(2027, 7, 5),
            date_to=date(2027, 7, 7),
        )
        colleague_req = await create_request(db, tenant.id, colleague.id, colleague_data)
        await approve_request(db, tenant.id, colleague_req.id, approver_id)

        # Employee submits for the same week
        employee_data = AbsenceRequestCreate(
            request_type=RequestType.VACATION,
            date_from=date(2027, 7, 5),
            date_to=date(2027, 7, 9),
        )
        emp_req = await create_request(db, tenant.id, employee.id, employee_data)

        overlapping = await _get_overlapping(
            db, tenant.id, emp_req.id, emp_req.date_from, emp_req.date_to
        )
        assert len(overlapping) == 1
        assert overlapping[0].employee_id == colleague.id
        assert overlapping[0].status == RequestStatus.APPROVED

    async def test_pending_colleague_also_included(
        self, db: AsyncSession, tenant: Tenant, employee: User
    ) -> None:
        colleague = await _make_employee(db, tenant, "Ben", "Meier")

        colleague_data = AbsenceRequestCreate(
            request_type=RequestType.VACATION,
            date_from=date(2027, 8, 2),
            date_to=date(2027, 8, 6),
        )
        await create_request(db, tenant.id, colleague.id, colleague_data)
        # the created request is SUBMITTED (not yet approved)

        employee_data = AbsenceRequestCreate(
            request_type=RequestType.VACATION,
            date_from=date(2027, 8, 4),
            date_to=date(2027, 8, 8),
        )
        emp_req = await create_request(db, tenant.id, employee.id, employee_data)

        overlapping = await _get_overlapping(
            db, tenant.id, emp_req.id, emp_req.date_from, emp_req.date_to
        )
        assert len(overlapping) == 1
        assert overlapping[0].status == RequestStatus.SUBMITTED

    async def test_non_overlapping_request_excluded(
        self, db: AsyncSession, tenant: Tenant, employee: User
    ) -> None:
        colleague = await _make_employee(db, tenant, "Clara", "Braun")
        approver_id = uuid.uuid4()

        # Colleague's vacation is well outside the employee's requested period
        colleague_data = AbsenceRequestCreate(
            request_type=RequestType.VACATION,
            date_from=date(2027, 9, 6),
            date_to=date(2027, 9, 10),
        )
        colleague_req = await create_request(db, tenant.id, colleague.id, colleague_data)
        await approve_request(db, tenant.id, colleague_req.id, approver_id)

        employee_data = AbsenceRequestCreate(
            request_type=RequestType.VACATION,
            date_from=date(2027, 7, 5),
            date_to=date(2027, 7, 9),
        )
        emp_req = await create_request(db, tenant.id, employee.id, employee_data)

        overlapping = await _get_overlapping(
            db, tenant.id, emp_req.id, emp_req.date_from, emp_req.date_to
        )
        assert overlapping == []

    async def test_staffing_percentage_calculation(
        self, db: AsyncSession, tenant: Tenant, employee: User
    ) -> None:
        """Staffing % = (total_active - absent_including_requester) / total_active * 100."""
        # We have 1 active employee (from fixture). Add 3 more.
        colleagues = [await _make_employee(db, tenant, f"Emp{i}", "Test") for i in range(3)]
        # total_active = 4

        approver_id = uuid.uuid4()

        # 1 colleague approved absent during the period
        absent_data = AbsenceRequestCreate(
            request_type=RequestType.VACATION,
            date_from=date(2027, 10, 4),
            date_to=date(2027, 10, 8),
        )
        absent_req = await create_request(db, tenant.id, colleagues[0].id, absent_data)
        await approve_request(db, tenant.id, absent_req.id, approver_id)

        # Employee submits for the same week
        emp_data = AbsenceRequestCreate(
            request_type=RequestType.VACATION,
            date_from=date(2027, 10, 4),
            date_to=date(2027, 10, 8),
        )
        emp_req = await create_request(db, tenant.id, employee.id, emp_data)

        overlapping = await _get_overlapping(
            db, tenant.id, emp_req.id, emp_req.date_from, emp_req.date_to
        )
        approved_ids = {r.employee_id for r in overlapping if r.status == RequestStatus.APPROVED}
        approved_ids.add(employee.id)  # include requester as if approved
        absent_during_period = len(approved_ids)

        total_active = 4  # employee + 3 colleagues
        staffing_pct = (total_active - absent_during_period) / total_active * 100

        # 2 absent out of 4 = 50% staffing → warning expected
        assert absent_during_period == 2
        assert staffing_pct == 50.0
        assert staffing_pct < 75.0  # warning threshold

    async def test_staffing_warning_threshold(self) -> None:
        """Warning generated when staffing < 75%."""
        total = 8
        absent = 3
        pct = (total - absent) / total * 100  # 62.5%
        warning = f"Kritisch: nur {pct:.0f}% Besetzung" if pct < 75.0 else None
        assert warning == "Kritisch: nur 62% Besetzung"

    async def test_no_warning_above_threshold(self) -> None:
        """No warning when staffing >= 75%."""
        total = 8
        absent = 1
        pct = (total - absent) / total * 100  # 87.5%
        warning = f"Kritisch: nur {pct:.0f}% Besetzung" if pct < 75.0 else None
        assert warning is None

    async def test_vacation_balance_for_requester_year(
        self, db: AsyncSession, tenant: Tenant, employee: User
    ) -> None:
        approver_id = uuid.uuid4()
        data = AbsenceRequestCreate(
            request_type=RequestType.VACATION,
            date_from=date(2027, 7, 5),
            date_to=date(2027, 7, 9),
        )
        req = await create_request(db, tenant.id, employee.id, data)
        await approve_request(db, tenant.id, req.id, approver_id)

        balance = await get_vacation_balance(db, tenant.id, employee.id, req.date_from.year)
        assert balance.year == 2027
        assert balance.used_days == 5.0
        # Neue Definition (2026-08-18): nur GENOMMENE Tage druecken den Rest;
        # der 2027er-Urlaub ist komplett geplant.
        assert balance.remaining_days == 24.0

    async def test_request_not_from_own_tenant_excluded(
        self, db: AsyncSession, tenant: Tenant, employee: User
    ) -> None:
        """Requests from other tenants must not appear in overlap results."""
        now = datetime.now(tz=UTC)
        other_tenant = Tenant(
            name="Other GmbH",
            timezone="Europe/Berlin",
            locale="de-DE",
            bundesland="BY",
            is_active=True,
            created_at=now,
            updated_at=now,
        )
        db.add(other_tenant)
        await db.flush()

        other_emp = await _make_employee(db, other_tenant, "Foreign", "User")
        approver_id = uuid.uuid4()

        other_data = AbsenceRequestCreate(
            request_type=RequestType.VACATION,
            date_from=date(2027, 11, 1),
            date_to=date(2027, 11, 5),
        )
        other_req = await create_request(db, other_tenant.id, other_emp.id, other_data)
        await approve_request(db, other_tenant.id, other_req.id, approver_id)

        emp_data = AbsenceRequestCreate(
            request_type=RequestType.VACATION,
            date_from=date(2027, 11, 1),
            date_to=date(2027, 11, 5),
        )
        emp_req = await create_request(db, tenant.id, employee.id, emp_data)

        # Overlap query scoped to tenant.id — other tenant's request must not appear
        overlapping = await _get_overlapping(
            db, tenant.id, emp_req.id, emp_req.date_from, emp_req.date_to
        )
        assert all(r.tenant_id == tenant.id for r in overlapping)
        assert not any(r.employee_id == other_emp.id for r in overlapping)
