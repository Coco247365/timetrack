"""Kiosk-Kurzmeldungen: Verwaltung + Abruf (#189)."""

from __future__ import annotations

from datetime import date

import pytest

from zeiterfassung.services import kiosk_message_service as svc


@pytest.mark.asyncio
async def test_broadcast_und_persoenlich_getrennt(db, tenant, employee):
    await svc.create_message(
        db,
        tenant.id,
        text="Weihnachtsfeier 24.12.",
        employee_id=None,
        active_until=None,
        created_by_account_id=None,
    )
    await svc.create_message(
        db,
        tenant.id,
        text="Betriebsarzt 10 Uhr",
        employee_id=employee.id,
        active_until=None,
        created_by_account_id=None,
    )
    today = date(2026, 7, 24)

    broadcasts = await svc.active_broadcasts(db, tenant.id, today)
    assert broadcasts == ["Weihnachtsfeier 24.12."]

    personal = await svc.active_for_employee(db, tenant.id, employee.id, today)
    assert personal == ["Betriebsarzt 10 Uhr"]

    # Rundmeldung darf NICHT als persoenliche Meldung auftauchen und umgekehrt.
    other = await svc.active_for_employee(db, tenant.id, tenant.id, today)  # fremde id
    assert other == []


@pytest.mark.asyncio
async def test_active_until_laeuft_ab(db, tenant):
    await svc.create_message(
        db,
        tenant.id,
        text="Abgelaufen",
        employee_id=None,
        active_until=date(2026, 7, 20),
        created_by_account_id=None,
    )
    await svc.create_message(
        db,
        tenant.id,
        text="Noch aktiv",
        employee_id=None,
        active_until=date(2026, 7, 30),
        created_by_account_id=None,
    )
    today = date(2026, 7, 24)
    assert await svc.active_broadcasts(db, tenant.id, today) == ["Noch aktiv"]


@pytest.mark.asyncio
async def test_loeschen(db, tenant):
    m = await svc.create_message(
        db,
        tenant.id,
        text="weg damit",
        employee_id=None,
        active_until=None,
        created_by_account_id=None,
    )
    import uuid as _uuid

    await svc.delete_message(db, tenant.id, _uuid.UUID(m["id"]))
    assert await svc.active_broadcasts(db, tenant.id, date(2026, 7, 24)) == []


@pytest.mark.asyncio
async def test_leerer_text_abgelehnt(db, tenant):
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        await svc.create_message(
            db,
            tenant.id,
            text="   ",
            employee_id=None,
            active_until=None,
            created_by_account_id=None,
        )


@pytest.mark.asyncio
async def test_list_broadcasts_only(db, tenant, employee):
    await svc.create_message(
        db,
        tenant.id,
        text="Rund",
        employee_id=None,
        active_until=None,
        created_by_account_id=None,
    )
    await svc.create_message(
        db,
        tenant.id,
        text="Pers",
        employee_id=employee.id,
        active_until=None,
        created_by_account_id=None,
    )
    only = await svc.list_messages(db, tenant.id, broadcasts_only=True)
    assert [m["text"] for m in only] == ["Rund"]


@pytest.mark.asyncio
async def test_active_from_planung_im_voraus(db, tenant):
    await svc.create_message(
        db,
        tenant.id,
        text="Zukunft",
        employee_id=None,
        active_from=date(2026, 8, 1),
        active_until=None,
        created_by_account_id=None,
    )
    await svc.create_message(
        db,
        tenant.id,
        text="Schon aktiv",
        employee_id=None,
        active_from=date(2026, 7, 1),
        active_until=date(2026, 7, 31),
        created_by_account_id=None,
    )
    # Vor dem Startdatum unsichtbar, ab Start sichtbar.
    assert await svc.active_broadcasts(db, tenant.id, date(2026, 7, 24)) == ["Schon aktiv"]
    assert await svc.active_broadcasts(db, tenant.id, date(2026, 8, 1)) == ["Zukunft"]
