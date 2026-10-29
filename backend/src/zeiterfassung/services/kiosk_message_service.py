"""Kiosk-Kurzmeldungen: Verwaltung + Abruf (#189).

Rundmeldungen (``employee_id IS NULL``) erscheinen dauerhaft im
Ruhebildschirm des Terminals; persoenliche Meldungen reiten auf der
Stempel-Antwort mit. ``active_until`` (optional) begrenzt die Anzeige bis
einschliesslich diesem Tag.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from zeiterfassung.db.scoping import get_scoped, tenant_scoped
from zeiterfassung.models.kiosk_message import KioskMessage


def _to_dict(m: KioskMessage) -> dict:
    return {
        "id": str(m.id),
        "employee_id": str(m.employee_id) if m.employee_id else None,
        "text": m.text,
        "active_from": m.active_from.isoformat() if m.active_from else None,
        "active_until": m.active_until.isoformat() if m.active_until else None,
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }


def _active(m: KioskMessage, today: date) -> bool:
    started = m.active_from is None or m.active_from <= today
    not_expired = m.active_until is None or m.active_until >= today
    return started and not_expired


async def list_messages(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    employee_id: uuid.UUID | None = None,
    broadcasts_only: bool = False,
) -> list[dict]:
    """Meldungen fuer die Weboberflaeche (neueste zuerst).

    - ``broadcasts_only``: nur Rundmeldungen (employee_id IS NULL).
    - ``employee_id``: nur die persoenlichen Meldungen dieses Mitarbeiters.
    - sonst: alle Meldungen des Mandanten.
    """
    stmt = tenant_scoped(select(KioskMessage), KioskMessage, tenant_id)
    if broadcasts_only:
        stmt = stmt.where(KioskMessage.employee_id.is_(None))
    elif employee_id is not None:
        stmt = stmt.where(KioskMessage.employee_id == employee_id)
    stmt = stmt.order_by(KioskMessage.created_at.desc())
    rows = (await db.execute(stmt)).scalars().all()
    return [_to_dict(m) for m in rows]


async def create_message(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    text: str,
    employee_id: uuid.UUID | None,
    active_until: date | None,
    created_by_account_id: uuid.UUID | None,
    active_from: date | None = None,
) -> dict:
    text = (text or "").strip()
    if not text:
        from fastapi import HTTPException, status

        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Text ist Pflicht.")
    text = text[:280]
    # Ein gesetztes employee_id muss zum Mandanten gehoeren.
    if employee_id is not None:
        from zeiterfassung.models.user import User

        if await get_scoped(db, User, tenant_id, employee_id) is None:
            from fastapi import HTTPException, status

            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Mitarbeiter nicht gefunden."
            )
    m = KioskMessage(
        tenant_id=tenant_id,
        employee_id=employee_id,
        text=text,
        active_from=active_from,
        active_until=active_until,
        created_by_account_id=created_by_account_id,
    )
    db.add(m)
    await db.commit()
    return _to_dict(m)


async def delete_message(db: AsyncSession, tenant_id: uuid.UUID, message_id: uuid.UUID) -> None:
    m = await get_scoped(db, KioskMessage, tenant_id, message_id)
    if m is None:
        from fastapi import HTTPException, status

        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Meldung nicht gefunden.")
    await db.delete(m)
    await db.commit()


async def active_broadcasts(db: AsyncSession, tenant_id: uuid.UUID, today: date) -> list[str]:
    """Aktive Rundmeldungen (Ruhebildschirm), aelteste zuerst."""
    stmt = (
        tenant_scoped(select(KioskMessage), KioskMessage, tenant_id)
        .where(KioskMessage.employee_id.is_(None))
        .order_by(KioskMessage.created_at.asc())
    )
    rows = (await db.execute(stmt)).scalars().all()
    return [m.text for m in rows if _active(m, today)]


async def active_for_employee(
    db: AsyncSession, tenant_id: uuid.UUID, employee_id: uuid.UUID, today: date
) -> list[str]:
    """Aktive persoenliche Meldungen fuer einen Mitarbeiter (Stempel-Antwort)."""
    stmt = (
        tenant_scoped(select(KioskMessage), KioskMessage, tenant_id)
        .where(KioskMessage.employee_id == employee_id)
        .order_by(KioskMessage.created_at.asc())
    )
    rows = (await db.execute(stmt)).scalars().all()
    return [m.text for m in rows if _active(m, today)]
