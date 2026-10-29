"""Tests for team_capacity_check service function."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from zeiterfassung.models.absence import RequestType
from zeiterfassung.models.department import Department
from zeiterfassung.models.user import User
from zeiterfassung.schemas.absence import AbsenceRequestCreate
from zeiterfassung.services.absence_service import (
    approve_request,
    cancel_request,
    create_request,
    reject_request,
    team_capacity_check,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def department(db: AsyncSession, tenant) -> Department:
    now = datetime.now(tz=UTC)
    dept = Department(
        tenant_id=tenant.id,
        name="Engineering",
        code="ENG",
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    db.add(dept)
    await db.flush()
    return dept


async def _make_employee(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    department_id: uuid.UUID | None,
    first_name: str,
    email_suffix: str,
    is_active: bool = True,
) -> User:
    from zeiterfassung.core.security import hash_password

    now = datetime.now(tz=UTC)
    u = User(
        tenant_id=tenant_id,
        email=f"{email_suffix}@test.de",
        hashed_password=hash_password("Passw0rd!"),
        first_name=first_name,
        last_name="Test",
        contracted_hours_per_week=40.0,
        department_id=department_id,
        is_active=is_active,
        created_at=now,
        updated_at=now,
    )
    db.add(u)
    await db.flush()
    return u


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTeamCapacityCheck:
    async def test_returns_team_count_and_absent(
        self, db: AsyncSession, tenant, department
    ) -> None:
        """Department with 5 employees, 2 on approved vacation — expect absent_count=2, pct=40."""
        approver_id = uuid.uuid4()
        employees = []
        for i in range(5):
            emp = await _make_employee(db, tenant.id, department.id, f"User{i}", f"user{i}.eng")
            employees.append(emp)

        # Put 2 employees on approved vacation during the check window
        for emp in employees[:2]:
            data = AbsenceRequestCreate(
                request_type=RequestType.VACATION,
                date_from=date(2028, 6, 10),
                date_to=date(2028, 6, 14),
            )
            req = await create_request(db, tenant.id, emp.id, data)
            await approve_request(db, tenant.id, req.id, approver_id)

        result = await team_capacity_check(
            db,
            tenant.id,
            department.id,
            date_from=date(2028, 6, 10),
            date_to=date(2028, 6, 14),
        )

        assert result["total_employees"] == 5
        assert result["absent_count"] == 2
        assert result["capacity_pct"] == 40.0
        assert len(result["absent_names"]) == 2

    async def test_excludes_own_requests(self, db: AsyncSession, tenant, department) -> None:
        """Employee checking their own dates should not count themselves in absent_count."""
        approver_id = uuid.uuid4()
        emp = await _make_employee(db, tenant.id, department.id, "Self", "self.eng")
        other = await _make_employee(db, tenant.id, department.id, "Other", "other.eng")

        # Both employees submit vacation for the same week
        for user in (emp, other):
            data = AbsenceRequestCreate(
                request_type=RequestType.VACATION,
                date_from=date(2028, 7, 1),
                date_to=date(2028, 7, 5),
            )
            req = await create_request(db, tenant.id, user.id, data)
            await approve_request(db, tenant.id, req.id, approver_id)

        # Checking as `emp` — their own request must not be counted
        result = await team_capacity_check(
            db,
            tenant.id,
            department.id,
            date_from=date(2028, 7, 1),
            date_to=date(2028, 7, 5),
            exclude_employee_id=emp.id,
        )

        assert result["total_employees"] == 2
        assert result["absent_count"] == 1
        assert result["absent_names"] == ["Other"]

    async def test_no_department_returns_zero(self, db: AsyncSession, tenant) -> None:
        """When department_id is a non-existent UUID the result should be zeros."""
        nonexistent_dept = uuid.uuid4()
        result = await team_capacity_check(
            db,
            tenant.id,
            nonexistent_dept,
            date_from=date(2028, 8, 1),
            date_to=date(2028, 8, 5),
        )

        assert result["total_employees"] == 0
        assert result["absent_count"] == 0
        assert result["absent_names"] == []
        assert result["capacity_pct"] == 0.0

    async def test_only_counts_active_absences(self, db: AsyncSession, tenant, department) -> None:
        """Cancelled and rejected requests must not be counted."""
        approver_id = uuid.uuid4()
        emp_cancelled = await _make_employee(
            db, tenant.id, department.id, "Cancelled", "cancelled.eng"
        )
        emp_rejected = await _make_employee(
            db, tenant.id, department.id, "Rejected", "rejected.eng"
        )
        emp_approved = await _make_employee(
            db, tenant.id, department.id, "Approved", "approved.eng"
        )

        # Cancelled request
        data = AbsenceRequestCreate(
            request_type=RequestType.VACATION,
            date_from=date(2028, 9, 2),
            date_to=date(2028, 9, 6),
        )
        req_cancelled = await create_request(db, tenant.id, emp_cancelled.id, data)
        await cancel_request(db, tenant.id, req_cancelled.id, emp_cancelled.id)

        # Rejected request
        data = AbsenceRequestCreate(
            request_type=RequestType.VACATION,
            date_from=date(2028, 9, 2),
            date_to=date(2028, 9, 6),
        )
        req_rejected = await create_request(db, tenant.id, emp_rejected.id, data)
        await reject_request(db, tenant.id, req_rejected.id, approver_id, "Understaffed")

        # Approved request — the only one that should count
        data = AbsenceRequestCreate(
            request_type=RequestType.VACATION,
            date_from=date(2028, 9, 2),
            date_to=date(2028, 9, 6),
        )
        req_approved = await create_request(db, tenant.id, emp_approved.id, data)
        await approve_request(db, tenant.id, req_approved.id, approver_id)

        result = await team_capacity_check(
            db,
            tenant.id,
            department.id,
            date_from=date(2028, 9, 2),
            date_to=date(2028, 9, 6),
        )

        assert result["total_employees"] == 3
        assert result["absent_count"] == 1
        assert result["absent_names"] == ["Approved"]

    async def test_submitted_absences_counted(self, db: AsyncSession, tenant, department) -> None:
        """SUBMITTED (pending) requests should also count as absent."""
        emp = await _make_employee(db, tenant.id, department.id, "Pending", "pending.eng")
        data = AbsenceRequestCreate(
            request_type=RequestType.VACATION,
            date_from=date(2028, 10, 7),
            date_to=date(2028, 10, 11),
        )
        await create_request(db, tenant.id, emp.id, data)

        result = await team_capacity_check(
            db,
            tenant.id,
            department.id,
            date_from=date(2028, 10, 7),
            date_to=date(2028, 10, 11),
        )

        assert result["absent_count"] == 1
        assert result["capacity_pct"] == 100.0

    async def test_inactive_employees_excluded_from_total(
        self, db: AsyncSession, tenant, department
    ) -> None:
        """Inactive employees should not be included in total_employees."""
        await _make_employee(db, tenant.id, department.id, "Active", "active.eng", is_active=True)
        await _make_employee(
            db, tenant.id, department.id, "Inactive", "inactive.eng", is_active=False
        )

        result = await team_capacity_check(
            db,
            tenant.id,
            department.id,
            date_from=date(2028, 11, 4),
            date_to=date(2028, 11, 8),
        )

        assert result["total_employees"] == 1

    async def test_date_range_fields_in_response(
        self, db: AsyncSession, tenant, department
    ) -> None:
        """Response must include date_from and date_to as ISO strings."""
        result = await team_capacity_check(
            db,
            tenant.id,
            department.id,
            date_from=date(2028, 12, 2),
            date_to=date(2028, 12, 6),
        )

        assert result["date_from"] == "2028-12-02"
        assert result["date_to"] == "2028-12-06"

    async def test_no_overlap_not_counted(self, db: AsyncSession, tenant, department) -> None:
        """Absence entirely outside the check window should not be counted."""
        approver_id = uuid.uuid4()
        emp = await _make_employee(db, tenant.id, department.id, "Away", "away.eng")

        # Absence in June, checking July
        data = AbsenceRequestCreate(
            request_type=RequestType.VACATION,
            date_from=date(2029, 6, 2),
            date_to=date(2029, 6, 6),
        )
        req = await create_request(db, tenant.id, emp.id, data)
        await approve_request(db, tenant.id, req.id, approver_id)

        result = await team_capacity_check(
            db,
            tenant.id,
            department.id,
            date_from=date(2029, 7, 7),
            date_to=date(2029, 7, 11),
        )

        assert result["absent_count"] == 0
        assert result["capacity_pct"] == 0.0
