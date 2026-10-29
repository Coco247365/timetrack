"""GET /absences/requests?employee_id=…: server-seitiger Mitarbeiter-Filter.

Ohne den Filter liefert der Endpoint tenant-weit nur die zuletzt erstellten
Anträge (limit, created_at DESC) — ältere Anträge eines Mitarbeiters fielen
auf der Detailseite aus dem Fenster, obwohl der Kalender sie zeigt.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from zeiterfassung.api.deps import ScopeContext
from zeiterfassung.models.absence import (
    ApprovalRequest,
    DayPart,
    RequestStatus,
    RequestType,
)
from zeiterfassung.services import absence_service


def _request(tenant, employee_id, *, day: date, created: datetime) -> ApprovalRequest:
    return ApprovalRequest(
        tenant_id=tenant.id,
        employee_id=employee_id,
        request_type=RequestType.VACATION,
        status=RequestStatus.APPROVED,
        date_from=day,
        date_to=day,
        day_part=DayPart.FULL,
        days_count=Decimal("1"),
        created_at=created,
        updated_at=created,
    )


@pytest.mark.asyncio
async def test_alter_antrag_wird_mit_employee_filter_gefunden(db: AsyncSession, tenant, employee):
    base = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    # 1 alter Antrag des Ziel-MA …
    db.add(_request(tenant, employee.id, day=date(2026, 6, 25), created=base))
    # … und 60 NEUERE Anträge anderer Mitarbeiter (verdrängen ihn aus limit=50).
    other = uuid.uuid4()
    for i in range(60):
        db.add(
            _request(tenant, other, day=date(2026, 7, 1), created=base + timedelta(minutes=i + 1))
        )
    await db.flush()

    scope = ScopeContext(scope="global", tenant_id=tenant.id, user_id=None)

    # Ohne employee_id (Alt-Verhalten der Detailseite): Antrag fehlt.
    ohne = await absence_service.list_requests(db, tenant.id, scope=scope, limit=50)
    assert all(r.employee_id != employee.id for r in ohne)

    # Mit employee_id: Antrag ist da.
    mit = await absence_service.list_requests(
        db, tenant.id, employee_id=employee.id, scope=scope, limit=50
    )
    assert [r.employee_id for r in mit] == [employee.id]
    assert mit[0].date_from == date(2026, 6, 25)


@pytest.mark.asyncio
async def test_self_scope_mit_fremder_id_liefert_nichts(db: AsyncSession, tenant, employee):
    base = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    fremd = uuid.uuid4()
    db.add(_request(tenant, fremd, day=date(2026, 7, 2), created=base))
    await db.flush()

    # Mitarbeiter-Scope (nur eigene) + fremde employee_id → AND-Filter ⇒ leer.
    scope = ScopeContext(scope="self", tenant_id=tenant.id, user_id=employee.id)
    got = await absence_service.list_requests(
        db, tenant.id, employee_id=fremd, scope=scope, limit=50
    )
    assert got == []


@pytest.mark.asyncio
async def test_status_filter_findet_alte_offene_antraege(db: AsyncSession, tenant, employee):
    """Genehmigungsliste: alter SUBMITTED-Antrag darf nicht aus dem
    created_at-Fenster fallen, wenn per Status gefiltert wird."""
    base = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    alt = _request(tenant, employee.id, day=date(2026, 6, 1), created=base)
    alt.status = RequestStatus.SUBMITTED
    db.add(alt)
    # 60 neuere, bereits entschiedene Anträge anderer verdrängen ihn aus limit=50.
    for i in range(60):
        created = base + timedelta(minutes=i + 1)
        db.add(_request(tenant, uuid.uuid4(), day=date(2026, 7, 1), created=created))
    await db.flush()

    scope = ScopeContext(scope="global", tenant_id=tenant.id, user_id=None)

    ohne = await absence_service.list_requests(db, tenant.id, scope=scope, limit=50)
    assert all(r.status != RequestStatus.SUBMITTED for r in ohne)

    offen = await absence_service.list_requests(
        db, tenant.id, status=RequestStatus.SUBMITTED, scope=scope, limit=50
    )
    assert [r.employee_id for r in offen] == [employee.id]
