"""Tenant provisioning primitives (zeiterfassung-65l).

A freshly created tenant cannot export today: no ``Company`` rows (Berater-/
Mandanten-Nr.), no ``WageTypeMapping`` rows, empty
``tenant.settings["lohnart_zuordnung"]``. ``export_service`` fails closed on
each of these (see ``_lodas_config_for_company`` / ``_get_wage_type_mappings``)
-- correctly, but that means onboarding tenant #2 needs a way to create
exactly those rows without forking a per-tenant seed script per tenant.

``provision_tenant`` is that mechanism. It is deliberately dumb: given
company/wage-type/Lohnart-Zuordnung data, it creates the rows an export
needs. It does NOT know or decide what any Lohnart number, rate, or mapping
should be -- that is HR-owned domain data (see
"Team-territory
issues"). Callers (e.g. ``a tenant-specific seed script, a future
onboarding script/endpoint for tenant #2) supply the concrete data; this
module only turns "this config" into "these rows", idempotently and strictly
scoped to the given tenant.

Append-only note: everything here is CONFIG (``companies``,
``wage_type_mappings``, ``Tenant.settings``), never ``punch_events`` or a
ledger table -- the append-only/audited rule
(``.claude/rules/append-only-ledger-and-events.md``) does not apply to these
rows, and this module must never be extended to touch those tables.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from zeiterfassung.db.scoping import tenant_scoped
from zeiterfassung.models.company import Company
from zeiterfassung.models.export import WageTypeMapping
from zeiterfassung.models.tenant import Tenant


@dataclass
class ProvisioningResult:
    """Summary of the rows ``provision_tenant`` created/updated."""

    companies: list[Company] = field(default_factory=list)
    wage_types_created: int = 0
    wage_types_updated: int = 0
    wage_types_removed: int = 0
    lohnart_zuordnung: dict[str, str] = field(default_factory=dict)


async def upsert_companies(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    company_data: Sequence[Mapping[str, object]],
) -> list[Company]:
    """Create/update ``Company`` rows (Berater-/Mandanten-Nr.) for a tenant.

    Upsert key: ``(tenant_id, name)``. Strictly scoped to ``tenant_id`` --
    never reads or writes another tenant's companies. Never deletes: a
    company missing from ``company_data`` on a re-run is left untouched
    (idempotent, non-destructive).

    Each ``company_data`` entry: ``{"name": str, "advisor_number": str | None,
    "client_number": str | None, "is_active": bool = True}``.
    """
    now = datetime.now(UTC)
    result: list[Company] = []
    for spec in company_data:
        name = spec.get("name")
        if not name:
            raise ValueError("company_data entry requires 'name'")
        row = (
            await db.execute(
                tenant_scoped(select(Company), Company, tenant_id).where(Company.name == name)
            )
        ).scalar_one_or_none()
        if row is None:
            row = Company(tenant_id=tenant_id, name=str(name), created_at=now, updated_at=now)
            db.add(row)
        row.is_active = bool(spec.get("is_active", True))
        row.advisor_number = spec.get("advisor_number")  # type: ignore[assignment]
        row.client_number = spec.get("client_number")  # type: ignore[assignment]
        result.append(row)
    await db.flush()
    return result


async def upsert_wage_type_mappings(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    wage_map: Sequence[Mapping[str, object]],
    *,
    replace: bool = False,
) -> tuple[int, int, int]:
    """Create/update ``WageTypeMapping`` rows (internal code -> DATEV Lohnart).

    Upsert key: ``(tenant_id, internal_code)``. Strictly scoped to
    ``tenant_id`` -- every read/write is filtered by it, so this can never
    touch another tenant's catalog (the bug fixed in
    ``scripts/seed_lohnarten_aj.py``: it used to ``SELECT id FROM tenants
    LIMIT 1`` and blind-delete across the whole table).

    ``replace=False`` (default, non-destructive): rows whose
    ``internal_code`` is not present in ``wage_map`` are left untouched --
    safe default for provisioning a fresh tenant or re-running a seed.
    ``replace=True`` (opt-in): additionally removes rows scoped to THIS
    tenant whose ``internal_code`` is not present in ``wage_map`` -- mirrors
    the old seed script's "Aufraeumen" step, but scoped + opt-in instead of a
    blind multi-tenant delete.

    Each ``wage_map`` entry: ``{"internal_code": str, "datev_lohnart":
    int | None, "description": str | None, "unit": str = "hours",
    "is_active": bool = True, "folge_lohnart": str | None}``.

    Returns ``(created, updated, removed)`` counts.
    """
    now = datetime.now(UTC)
    existing_rows = (
        (await db.execute(tenant_scoped(select(WageTypeMapping), WageTypeMapping, tenant_id)))
        .scalars()
        .all()
    )
    existing = {r.internal_code: r for r in existing_rows}

    created = updated = removed = 0
    seeded_codes: set[str] = set()
    for entry in wage_map:
        code = entry.get("internal_code")
        if not code:
            raise ValueError("wage_map entry requires 'internal_code'")
        code = str(code)
        seeded_codes.add(code)
        row = existing.get(code)
        if row is None:
            db.add(
                WageTypeMapping(
                    tenant_id=tenant_id,
                    internal_code=code,
                    datev_lohnart=entry.get("datev_lohnart"),  # type: ignore[arg-type]
                    description=entry.get("description"),  # type: ignore[arg-type]
                    is_active=bool(entry.get("is_active", True)),
                    unit=str(entry.get("unit") or "hours"),
                    folge_lohnart=entry.get("folge_lohnart"),  # type: ignore[arg-type]
                    created_at=now,
                )
            )
            created += 1
        else:
            row.datev_lohnart = entry.get("datev_lohnart")  # type: ignore[assignment]
            row.description = entry.get("description")  # type: ignore[assignment]
            row.is_active = bool(entry.get("is_active", True))
            row.unit = str(entry.get("unit") or "hours")
            row.folge_lohnart = entry.get("folge_lohnart")  # type: ignore[assignment]
            updated += 1

    if replace:
        for code, row in existing.items():
            if code not in seeded_codes:
                await db.delete(row)
                removed += 1

    await db.flush()
    return created, updated, removed


def merge_lohnart_zuordnung(tenant: Tenant, lohnart_zuordnung: Mapping[str, str]) -> dict[str, str]:
    """Merge category->internal_code assignments into ``tenant.settings``.

    Merges rather than overwrites, so re-running provisioning (or seeding a
    second catalog) never clobbers assignments already made via the
    Einstellungen UI. Mutates ``tenant.settings`` in place (caller flushes).
    """
    settings = dict(tenant.settings or {})
    zuordnung = dict(settings.get("lohnart_zuordnung") or {})
    zuordnung.update(lohnart_zuordnung)
    settings["lohnart_zuordnung"] = zuordnung
    tenant.settings = settings
    return zuordnung


async def provision_tenant(
    db: AsyncSession,
    *,
    tenant: Tenant,
    company_data: Sequence[Mapping[str, object]],
    wage_map: Sequence[Mapping[str, object]],
    lohnart_zuordnung: Mapping[str, str],
) -> ProvisioningResult:
    """Make a fresh tenant export-capable. Mechanism only -- no domain data.

    Creates exactly the rows ``export_service`` requires for a DATEV/LODAS
    export to succeed instead of failing on missing config:

    - ``Company`` rows (Berater-/Mandanten-Nr.), consumed by
      ``export_service._lodas_config_for_company``.
    - ``WageTypeMapping`` rows (internal Lohnart catalog), consumed by
      ``export_service._get_wage_type_mappings`` / ``_build_datev_rows``.
    - ``Tenant.settings["lohnart_zuordnung"]`` (category -> internal_code),
      consumed by the same function.

    CRITICAL BOUNDARY: ``company_data``/``wage_map``/``lohnart_zuordnung`` are
    supplied by the CALLER. This function never hardcodes a Lohn value,
    rate, or business rule -- it only knows how to turn "this config" into
    "these rows". Idempotent (safe to re-run): existing rows are updated in
    place, never blind-deleted (``upsert_wage_type_mappings`` runs with
    ``replace=False``).
    """
    companies = await upsert_companies(db, tenant.id, company_data)
    created, updated, removed = await upsert_wage_type_mappings(
        db, tenant.id, wage_map, replace=False
    )
    zuordnung = merge_lohnart_zuordnung(tenant, lohnart_zuordnung)
    await db.flush()
    return ProvisioningResult(
        companies=companies,
        wage_types_created=created,
        wage_types_updated=updated,
        wage_types_removed=removed,
        lohnart_zuordnung=zuordnung,
    )
