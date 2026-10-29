"""Überstunden-Konto (Saldo / Log / manuelle Korrektur).

Split out of the former monolithic ``api/v1/users.py`` (zeiterfassung-6pcv).
Mounted at the same absolute paths as before (this router carries no prefix —
see ``api/v1/users/__init__.py``).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status

from zeiterfassung.api.deps import AuthUser, DBSession
from zeiterfassung.api.v1.users._permissions import admin_can_read, can_manage_overtime
from zeiterfassung.services import overtime_service

router = APIRouter()


@router.get("/overtime/balance", response_model=dict)
async def overtime_balance(employee_id: uuid.UUID, db: DBSession, current_user: AuthUser) -> dict:
    """Aktueller Überstunden-Saldo + Auszahl-Modus eines Mitarbeiters.

    Eigener Saldo ist immer erlaubt (Self-Service); fremde nur mit Admin-Read.
    """
    is_self = current_user.employee_id is not None and employee_id == current_user.employee_id
    if not is_self and not admin_can_read(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Admin read permission required"
        )
    return await overtime_service.balance_for_employee(db, current_user.tenant_id, employee_id)


@router.get("/overtime/balances", response_model=dict)
async def overtime_balances(db: DBSession, current_user: AuthUser) -> dict:
    """Überstunden-Saldo (h) je Mitarbeiter (für Listen)."""
    if not admin_can_read(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Admin read permission required"
        )
    balances = await overtime_service.live_balances_for_tenant(db, current_user.tenant_id)
    return {str(eid): float(bal) for eid, bal in balances.items()}


@router.get("/overtime/ledger", response_model=list[dict])
async def overtime_ledger(
    employee_id: uuid.UUID, db: DBSession, current_user: AuthUser
) -> list[dict]:
    """Überstunden-Log eines Mitarbeiters (chronologisch). Eigenes Log immer erlaubt."""
    is_self = current_user.employee_id is not None and employee_id == current_user.employee_id
    if not is_self and not admin_can_read(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Admin read permission required"
        )
    return await overtime_service.ledger_for_employee(db, current_user.tenant_id, employee_id)


@router.get("/overtime/daily", response_model=list[dict])
async def overtime_daily(
    employee_id: uuid.UUID, db: DBSession, current_user: AuthUser
) -> list[dict]:
    """Tages-Verlauf des Arbeitszeitkontos ab Stichtag (Ist/Soll/Δ + laufender Saldo)."""
    is_self = current_user.employee_id is not None and employee_id == current_user.employee_id
    if not is_self and not admin_can_read(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Admin read permission required"
        )
    return await overtime_service.daily_for_employee(db, current_user.tenant_id, employee_id)


@router.get("/overtime/month-summary", response_model=dict)
async def overtime_month_summary(
    employee_id: uuid.UUID, year: int, month: int, db: DBSession, current_user: AuthUser
) -> dict:
    """Monats-Summen fürs Kalender-Kopfzeilen-Trio: Ist, effektives Soll, Saldo.

    Tagesgenau über die Konto-Policy (Feiertage/Abwesenheiten reduzieren das
    Soll, Zukunftstage zählen nicht). Eigener Monat immer erlaubt.
    """
    if month < 1 or month > 12:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="month muss 1–12 sein"
        )
    is_self = current_user.employee_id is not None and employee_id == current_user.employee_id
    if not is_self and not admin_can_read(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Admin read permission required"
        )
    return await overtime_service.month_summary_for_employee(
        db, current_user.tenant_id, employee_id, year, month
    )


@router.post("/overtime/adjustment", response_model=dict, status_code=status.HTTP_201_CREATED)
async def overtime_adjustment(body: dict, db: DBSession, current_user: AuthUser) -> dict:
    """Manuelle Überstunden-Korrektur / Anfangsbestand (±h) ins Log."""
    if not can_manage_overtime(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="overtime:manage permission required"
        )
    return await overtime_service.create_adjustment(
        db, current_user.tenant_id, body, actor_employee_id=current_user.employee_id
    )


@router.patch("/overtime/adjustment/{entry_id}", response_model=dict)
async def overtime_adjustment_update(
    entry_id: uuid.UUID, body: dict, db: DBSession, current_user: AuthUser
) -> dict:
    """Eine manuelle Überstunden-Korrektur bearbeiten (nur adjustment-Einträge)."""
    if not can_manage_overtime(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="overtime:manage permission required"
        )
    return await overtime_service.update_adjustment_for_router(
        db, current_user.tenant_id, entry_id, body, actor_employee_id=current_user.employee_id
    )


@router.delete("/overtime/adjustment/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
async def overtime_adjustment_delete(
    entry_id: uuid.UUID, db: DBSession, current_user: AuthUser
) -> None:
    """Eine manuelle Überstunden-Korrektur löschen (nur adjustment-Einträge)."""
    if not can_manage_overtime(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="overtime:manage permission required"
        )
    await overtime_service.delete_adjustment_for_router(
        db, current_user.tenant_id, entry_id, actor_employee_id=current_user.employee_id
    )
