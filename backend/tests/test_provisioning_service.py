"""Acceptance test for zeiterfassung-65l: a fresh tenant becomes export-capable.

Before ``provision_tenant``, a freshly created tenant has no ``Company`` rows,
no ``WageTypeMapping`` rows, and an empty ``settings["lohnart_zuordnung"]`` --
``export_service`` fails closed on all three (see ``_lodas_config_for_company``
/ ``_get_wage_type_mappings``). This test provisions a fresh (empty) tenant
via ``provision_tenant(...)`` with caller-supplied config (no hardcoded Lohn
values) and then asserts export preconditions are satisfied end-to-end,
including a real ``generate_export`` run -- proving tenant #2 can be
onboarded without forking a per-tenant seed script.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select

from zeiterfassung.db.scoping import tenant_scoped
from zeiterfassung.models.clock import EventSource, EventType, PunchEvent
from zeiterfassung.models.company import Company
from zeiterfassung.models.export import WageTypeMapping
from zeiterfassung.schemas.export import DatevExportRequest
from zeiterfassung.services.export_service import (
    _get_wage_type_mappings,
    _lodas_config_for_company,
    generate_export,
)
from zeiterfassung.services.provisioning_service import provision_tenant

COMPANY_DATA = [
    {
        "name": "Demo Logistik",
        "advisor_number": "9001",
        "client_number": "10042",
    }
]
WAGE_MAP = [
    {
        "internal_code": "LA_0011",
        "datev_lohnart": 11,
        "description": "Zeitlohn (Anwesenheit)",
        "unit": "hours",
    },
    {
        "internal_code": "LA_0129",
        "datev_lohnart": 129,
        "description": "Urlaublohn",
        "unit": "hours",
    },
]
LOHNART_ZUORDNUNG = {
    "NORMALE_ARBEITSZEIT": "LA_0011",
    "URLAUB_STD": "LA_0129",
}


async def test_provision_tenant_creates_required_config_rows(db, tenant):
    """provision_tenant() creates exactly the Company/WageTypeMapping/settings
    rows export_service needs -- none of it pre-existed on the fresh tenant."""
    # Precondition: the fresh tenant fixture has none of this yet.
    assert (tenant.settings or {}).get("lohnart_zuordnung") in (None, {})
    existing_companies = (
        (await db.execute(tenant_scoped(select(Company), Company, tenant.id))).scalars().all()
    )
    assert existing_companies == []

    result = await provision_tenant(
        db,
        tenant=tenant,
        company_data=COMPANY_DATA,
        wage_map=WAGE_MAP,
        lohnart_zuordnung=LOHNART_ZUORDNUNG,
    )
    await db.commit()

    assert len(result.companies) == 1
    company = result.companies[0]
    assert company.tenant_id == tenant.id
    assert company.advisor_number == "9001"
    assert company.client_number == "10042"

    assert result.wage_types_created == 2
    assert result.wage_types_updated == 0
    assert result.wage_types_removed == 0
    assert result.lohnart_zuordnung == LOHNART_ZUORDNUNG
    assert (tenant.settings or {}).get("lohnart_zuordnung") == LOHNART_ZUORDNUNG


async def test_provision_tenant_is_idempotent(db, tenant):
    """Re-running provision_tenant with the same config never duplicates rows
    and never deletes anything (default is non-destructive upsert)."""
    await provision_tenant(
        db,
        tenant=tenant,
        company_data=COMPANY_DATA,
        wage_map=WAGE_MAP,
        lohnart_zuordnung=LOHNART_ZUORDNUNG,
    )
    await db.commit()

    result2 = await provision_tenant(
        db,
        tenant=tenant,
        company_data=COMPANY_DATA,
        wage_map=WAGE_MAP,
        lohnart_zuordnung=LOHNART_ZUORDNUNG,
    )
    await db.commit()

    assert result2.wage_types_created == 0
    assert result2.wage_types_updated == 2
    assert result2.wage_types_removed == 0

    companies = (
        (await db.execute(tenant_scoped(select(Company), Company, tenant.id))).scalars().all()
    )
    assert len(companies) == 1

    wage_rows = (
        (await db.execute(tenant_scoped(select(WageTypeMapping), WageTypeMapping, tenant.id)))
        .scalars()
        .all()
    )
    assert len(wage_rows) == 2


async def test_provision_tenant_never_touches_other_tenants(db, tenant, engine):
    """provision_tenant only ever creates/reads rows for the given tenant_id."""
    from datetime import UTC as _UTC
    from datetime import datetime as _dt

    from zeiterfassung.models.tenant import Tenant

    other = Tenant(
        name="Other GmbH",
        timezone="Europe/Berlin",
        locale="de-DE",
        bundesland="HH",
        is_active=True,
        created_at=_dt.now(tz=_UTC),
        updated_at=_dt.now(tz=_UTC),
    )
    db.add(other)
    await db.flush()

    await provision_tenant(
        db,
        tenant=tenant,
        company_data=COMPANY_DATA,
        wage_map=WAGE_MAP,
        lohnart_zuordnung=LOHNART_ZUORDNUNG,
    )
    await db.commit()

    other_companies = (
        (await db.execute(tenant_scoped(select(Company), Company, other.id))).scalars().all()
    )
    other_wage_rows = (
        (await db.execute(tenant_scoped(select(WageTypeMapping), WageTypeMapping, other.id)))
        .scalars()
        .all()
    )
    assert other_companies == []
    assert other_wage_rows == []
    assert (other.settings or {}).get("lohnart_zuordnung") in (None, {})


async def test_provisioned_tenant_satisfies_export_preconditions(db, tenant):
    """The narrow preconditions export_service checks are satisfied post-provisioning.

    Before provisioning, both of these raise ``ValueError`` (missing
    Berater-/Mandanten-Nr., empty wage-type catalog); after provisioning they
    resolve cleanly.
    """
    result = await provision_tenant(
        db,
        tenant=tenant,
        company_data=COMPANY_DATA,
        wage_map=WAGE_MAP,
        lohnart_zuordnung=LOHNART_ZUORDNUNG,
    )
    await db.commit()
    company = result.companies[0]

    # Was: raise ValueError("... fehlen Berater-Nr. und/oder Mandanten-Nr. ...")
    config = await _lodas_config_for_company(db, tenant.id, company.id)
    assert config.berater_nr == "9001"
    assert config.mandanten_nr == "10042"

    # Was: {} (no active WageTypeMapping rows, no lohnart_zuordnung).
    wage_types = await _get_wage_type_mappings(db, tenant.id)
    assert wage_types["NORMALE_ARBEITSZEIT"] == 11
    assert wage_types["URLAUB_STD"] == 129


async def test_fresh_tenant_without_company_still_fails_closed(db, tenant):
    """Sanity control: WITHOUT a company_id, generate_export still fails --
    proving the acceptance test above is testing something real, not a tautology."""
    req = DatevExportRequest(year=2026, month=6, company_id=None)
    with pytest.raises(ValueError, match="Firma"):
        await generate_export(db, tenant.id, req, generated_by=tenant.id)


async def test_provisioned_tenant_generate_export_succeeds(db, tenant, employee):
    """End-to-end: provision a fresh tenant, punch one day, generate a real
    LODAS export -- the actual proof tenant #2 can be onboarded."""
    result = await provision_tenant(
        db,
        tenant=tenant,
        company_data=COMPANY_DATA,
        wage_map=WAGE_MAP,
        lohnart_zuordnung=LOHNART_ZUORDNUNG,
    )
    company = result.companies[0]
    employee.company_id = company.id
    await db.flush()

    now = datetime.now(tz=UTC)
    for etype, hhmm in (
        (EventType.PUNCH_IN, "06:00"),
        (EventType.PUNCH_OUT, "14:00"),
    ):
        h, m = map(int, hhmm.split(":"))
        db.add(
            PunchEvent(
                tenant_id=tenant.id,
                employee_id=employee.id,
                event_type=etype,
                event_time=datetime(2026, 6, 15, h - 2, m, tzinfo=UTC),  # local CEST -> UTC
                source=EventSource.NFC_TERMINAL,
                created_at=now,
            )
        )
    await db.commit()

    req = DatevExportRequest(year=2026, month=6, company_id=company.id)
    export, file_bytes = await generate_export(db, tenant.id, req, generated_by=employee.id)
    await db.commit()

    assert export.tenant_id == tenant.id
    assert export.year_month == date(2026, 6, 1)
    assert export.row_count is not None and export.row_count > 0
    assert file_bytes
