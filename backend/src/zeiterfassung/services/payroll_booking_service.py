"""Manuelle Lohnbuchungen (Pauschalen / Beträge / Sonderzahlungen -> nur Export).

Moved verbatim out of the former monolithic ``api/v1/users.py``
(zeiterfassung-6pcv).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from zeiterfassung.db.scoping import get_scoped, tenant_scoped
from zeiterfassung.models.export import WageTypeMapping
from zeiterfassung.models.payroll_booking import PayrollBooking


async def list_bookings(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    employee_id: uuid.UUID | None,
    year: int | None,
    month: int | None,
) -> list[dict]:
    """Manuelle Lohnbuchungen (optional nach Mitarbeiter + Monat gefiltert)."""
    stmt = tenant_scoped(select(PayrollBooking), PayrollBooking, tenant_id)
    if employee_id is not None:
        stmt = stmt.where(PayrollBooking.employee_id == employee_id)
    if year and month:
        stmt = stmt.where(PayrollBooking.year_month == date(int(year), int(month), 1))
    rows = list((await db.execute(stmt.order_by(PayrollBooking.year_month.desc()))).scalars().all())

    wt = {
        w.internal_code: w
        for w in (
            await db.execute(tenant_scoped(select(WageTypeMapping), WageTypeMapping, tenant_id))
        )
        .scalars()
        .all()
    }
    result = []
    for b in rows:
        w = wt.get(b.wage_type_code)
        result.append(
            {
                "id": str(b.id),
                "employee_id": str(b.employee_id),
                "year_month": b.year_month.isoformat(),
                "booking_date": b.booking_date.isoformat() if b.booking_date else None,
                "wage_type_code": b.wage_type_code,
                "value": float(b.value),
                "note": b.note,
                "datev_lohnart": w.datev_lohnart if w else None,
                "description": w.description if w else None,
                "unit": w.unit if w else "hours",
            }
        )
    return result


async def create_booking(
    db: AsyncSession, tenant_id: uuid.UUID, body: dict, *, actor_employee_id: uuid.UUID | None
) -> dict:
    """Manuelle Lohnbuchung anlegen (Wert darf negativ sein = Abzug)."""
    from zeiterfassung.models.user import User

    emp = body.get("employee_id")
    code = (body.get("wage_type_code") or "").strip()
    year = body.get("year")
    month = body.get("month")
    if not emp or not code or not year or not month:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="employee_id, wage_type_code, year, month erforderlich",
        )
    if code == "UEBERSTUNDEN_AUSZAHLUNG":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Reservierter System-Code (wird vom Monatsabschluss erzeugt)",
        )
    try:
        value = Decimal(str(body.get("value")).replace(",", "."))
    except (InvalidOperation, TypeError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="value muss eine Zahl sein"
        ) from None

    # Lohnart muss existieren (DB-Katalog oder Standard-Katalog).
    w = (
        await db.execute(
            tenant_scoped(select(WageTypeMapping), WageTypeMapping, tenant_id).where(
                WageTypeMapping.internal_code == code,
            )
        )
    ).scalar_one_or_none()
    if w is None:
        from zeiterfassung.payroll.categories import all_categories

        if code not in {c.code for c in all_categories()}:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unbekannte Lohnart"
            )

    # Optionales Doku-Datum (nur Dokumentation, nicht im Export).
    booking_date = None
    bd = body.get("booking_date") or body.get("date")
    if bd:
        try:
            booking_date = date.fromisoformat(str(bd)[:10])
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="booking_date muss ein Datum (YYYY-MM-DD) sein",
            ) from None

    # Ziel-Mitarbeiter muss zum eigenen Mandanten gehoeren.
    ok = (
        await db.execute(
            tenant_scoped(select(User.id), User, tenant_id).where(User.id == uuid.UUID(str(emp)))
        )
    ).first()
    if ok is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Mitarbeiter nicht gefunden"
        )

    now = datetime.now(UTC)
    booking = PayrollBooking(
        tenant_id=tenant_id,
        employee_id=uuid.UUID(str(emp)),
        year_month=date(int(year), int(month), 1),
        booking_date=booking_date,
        wage_type_code=code,
        value=value,
        note=(body.get("note") or None),
        created_by=actor_employee_id,
        created_at=now,
        updated_at=now,
    )
    db.add(booking)
    await db.commit()
    return {"ok": True, "id": str(booking.id)}


async def delete_booking(db: AsyncSession, tenant_id: uuid.UUID, booking_id: uuid.UUID) -> None:
    """Manuelle Lohnbuchung löschen."""
    b = await get_scoped(db, PayrollBooking, tenant_id, booking_id)
    if b is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Booking not found")
    await db.delete(b)
    await db.commit()
