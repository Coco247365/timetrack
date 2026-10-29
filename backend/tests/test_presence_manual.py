"""Repro: a manually added clock-in (ADMIN_MANUAL) should mark the employee
present in the presence overview (status clocked_in + clocked_in_since)."""

from __future__ import annotations

from datetime import UTC, datetime

from zeiterfassung.api.deps import ScopeContext
from zeiterfassung.models.clock import EventType
from zeiterfassung.services.clock_service import (
    add_manual_event,
    get_current_clock_status,
    get_presence_overview,
    void_event,
)


async def test_manual_clockin_shows_present(db, tenant, employee) -> None:
    now = datetime.now(tz=UTC)
    today_8 = now.replace(hour=8, minute=12, second=0, microsecond=0)

    await add_manual_event(
        db, tenant.id, employee.id, EventType.PUNCH_IN, today_8, employee.id, "manuell"
    )

    scope = ScopeContext(scope="global", tenant_id=tenant.id, user_id=None)
    overview = await get_presence_overview(db, tenant.id, scope)

    emp = next(e for e in overview["employees"] if e["employee_id"] == employee.id)
    assert emp["status"] == "clocked_in", emp
    assert emp["clocked_in_since"] is not None, emp


async def test_voided_clockout_keeps_present(db, tenant, employee) -> None:
    """A clock-out that was added then deleted must not leave the employee 'out'."""
    now = datetime.now(tz=UTC)
    await add_manual_event(
        db,
        tenant.id,
        employee.id,
        EventType.PUNCH_IN,
        now.replace(hour=8, minute=12, second=0, microsecond=0),
        employee.id,
        "kommt",
    )
    geht = await add_manual_event(
        db,
        tenant.id,
        employee.id,
        EventType.PUNCH_OUT,
        now.replace(hour=17, minute=0, second=0, microsecond=0),
        employee.id,
        "geht",
    )
    await void_event(db, tenant.id, geht.id, employee.id, "doch nicht raus")

    # Presence (Liste) must show present again.
    scope = ScopeContext(scope="global", tenant_id=tenant.id, user_id=None)
    overview = await get_presence_overview(db, tenant.id, scope)
    emp = next(e for e in overview["employees"] if e["employee_id"] == employee.id)
    assert emp["status"] == "clocked_in", emp
    assert emp["clocked_in_since"] is not None, emp

    # Profile badge (get_current_clock_status) must agree.
    st = await get_current_clock_status(db, tenant.id, employee.id)
    assert st["is_clocked_in"] is True, st
    assert st["clocked_in_since"] is not None, st
