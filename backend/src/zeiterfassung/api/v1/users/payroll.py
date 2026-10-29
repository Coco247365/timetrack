"""Manuelle Lohnbuchungen (Pauschalen / Beträge / Sonderzahlungen -> nur Export).

Split out of the former monolithic ``api/v1/users.py`` (zeiterfassung-6pcv).
Mounted at the same absolute paths as before (this router carries no prefix —
see ``api/v1/users/__init__.py``).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status

from zeiterfassung.api.deps import AuthUser, DBSession
from zeiterfassung.services import payroll_booking_service

router = APIRouter()


def _can_read_payroll(u: AuthUser) -> bool:
    return (
        u.has_permission("payroll:read")
        or u.has_permission("payroll:write")
        or u.has_permission_namespace("admin")
    )


def _can_write_payroll(u: AuthUser) -> bool:
    return u.has_permission("payroll:write") or u.has_permission_namespace("admin")


@router.get("/payroll/bookings", response_model=list[dict])
async def list_payroll_bookings(
    db: DBSession,
    current_user: AuthUser,
    employee_id: uuid.UUID | None = None,
    year: int | None = None,
    month: int | None = None,
) -> list[dict]:
    """Manuelle Lohnbuchungen (optional nach Mitarbeiter + Monat gefiltert)."""
    if not _can_read_payroll(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="payroll:read permission required"
        )
    return await payroll_booking_service.list_bookings(
        db, current_user.tenant_id, employee_id, year, month
    )


@router.post("/payroll/bookings", response_model=dict, status_code=status.HTTP_201_CREATED)
async def create_payroll_booking(body: dict, db: DBSession, current_user: AuthUser) -> dict:
    """Manuelle Lohnbuchung anlegen (Wert darf negativ sein = Abzug)."""
    if not _can_write_payroll(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="payroll:write permission required"
        )
    return await payroll_booking_service.create_booking(
        db, current_user.tenant_id, body, actor_employee_id=current_user.employee_id
    )


@router.delete("/payroll/bookings/{booking_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_payroll_booking(
    booking_id: uuid.UUID, db: DBSession, current_user: AuthUser
) -> None:
    """Manuelle Lohnbuchung löschen."""
    if not _can_write_payroll(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="payroll:write permission required"
        )
    await payroll_booking_service.delete_booking(db, current_user.tenant_id, booking_id)
