"""Payroll/Lohnarten configuration endpoints: wage-type mappings, surcharge
config, category assignments, overtime rules + resettle, wage profiles.

Split out of the former monolithic ``api/v1/admin.py`` (zeiterfassung-6pcv).
Mounted under ``/admin`` (prefix applied once by ``api/v1/router.py`` via the
``admin`` package aggregator — see ``api/v1/admin/__init__.py``).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from zeiterfassung.api.deps import AuthUser, DBSession
from zeiterfassung.services import payroll_config_service

router = APIRouter()


def _require_settings_perm(admin: AuthUser) -> None:
    if not admin.has_permission("admin:settings"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions"
        )


@router.get("/wage-types", response_model=list[dict])
async def get_wage_types(db: DBSession, admin: AuthUser) -> list[dict]:
    """List wage-type mappings (internal code -> DATEV Lohnart) for the tenant.

    Requires ``admin:settings``. If no mappings exist in the DB yet, the canonical
    default set from ``AJO_WAGE_TYPES`` is returned as virtual rows so the admin can
    see the expected configuration.
    """
    _require_settings_perm(admin)
    return await payroll_config_service.list_wage_types(db, admin.tenant_id)


@router.post("/wage-types")
async def upsert_wage_type(body: dict, db: DBSession, admin: AuthUser) -> dict:
    """Create or update a wage-type mapping by (tenant_id, internal_code).

    Requires ``admin:settings``. Body fields: ``internal_code`` (str, required),
    ``datev_lohnart`` (int, required), ``description`` (str|None), ``is_active``
    (bool, default True).
    """
    _require_settings_perm(admin)
    return await payroll_config_service.upsert_wage_type(db, admin.tenant_id, body)


@router.delete("/wage-types/{internal_code}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_wage_type(internal_code: str, db: DBSession, admin: AuthUser) -> None:
    """Delete a wage-type mapping (by internal_code). Requires ``admin:settings``."""
    _require_settings_perm(admin)
    await payroll_config_service.delete_wage_type(db, admin.tenant_id, internal_code)


@router.post("/wage-types/seed-defaults")
async def seed_default_wage_types(db: DBSession, admin: AuthUser) -> dict:
    """Standard-Kategorien (ohne Nummern) in die DB uebernehmen.

    Requires ``admin:settings``. Only missing ``internal_code`` rows are created;
    existing rows are left untouched. Returns the number of rows created.
    """
    _require_settings_perm(admin)
    return await payroll_config_service.seed_default_wage_types(db, admin.tenant_id)


@router.get("/surcharge-config")
async def get_surcharge_config(db: DBSession, admin: AuthUser) -> dict:
    """Return the tenant's surcharge configuration (Zuschlagssaetze).

    Requires ``admin:settings``. Missing fields are filled with defaults
    (night_start=23, night_end=6, night_pct=25, sunday_pct=50,
    holiday_pct=125, saturday_pct=0).
    """
    _require_settings_perm(admin)
    return await payroll_config_service.get_surcharge_config(db, admin.tenant_id)


@router.post("/surcharge-config")
@router.patch("/surcharge-config")
async def set_surcharge_config(body: dict, db: DBSession, admin: AuthUser) -> dict:
    """Persist the tenant's surcharge configuration into ``Tenant.settings["zuschlaege"]``.

    Requires ``admin:settings``. Tolerant of missing fields (filled with defaults)
    and casts hour fields to int and percentage fields to float.
    """
    _require_settings_perm(admin)
    return await payroll_config_service.set_surcharge_config(db, admin.tenant_id, body)


@router.get("/wage-type-assignments")
async def get_wage_type_assignments(db: DBSession, admin: AuthUser) -> dict:
    """Zuordnung System-Kategorien -> Katalog-Lohnart (aus dem Steuerberater-PDF).

    Der Export berechnet die Kategorien automatisch und bucht sie auf die hier
    zugeordnete Lohnart. Ohne Zuordnung wird eine Kategorie nicht exportiert.
    """
    _require_settings_perm(admin)
    return await payroll_config_service.get_wage_type_assignments(db, admin.tenant_id)


@router.patch("/wage-type-assignments")
@router.post("/wage-type-assignments")
async def set_wage_type_assignments(body: dict, db: DBSession, admin: AuthUser) -> dict:
    """Zuordnungen speichern: Body {assignments: {CATEGORY_CODE: internal_code|null}}."""
    _require_settings_perm(admin)
    return await payroll_config_service.set_wage_type_assignments(db, admin.tenant_id, body)


@router.get("/overtime-config")
async def get_overtime_config(db: DBSession, admin: AuthUser) -> dict:
    """Return the tenant's overtime rules (Ueberstundenregeln).

    Requires ``admin:settings``. Missing fields are filled with defaults
    (max_balance_hours=120, expiry_months=12, allow_payout=True,
    allow_compensation=True).
    """
    _require_settings_perm(admin)
    return await payroll_config_service.get_overtime_config(db, admin.tenant_id)


@router.post("/overtime-config")
async def set_overtime_config(body: dict, db: DBSession, admin: AuthUser) -> dict:
    """Persist the tenant's overtime rules into ``Tenant.settings["ueberstunden"]``.

    Requires ``admin:settings``. Tolerant of missing fields (filled with defaults)
    and casts max_balance_hours to float, expiry_months to int and the flags to bool.
    """
    _require_settings_perm(admin)
    return await payroll_config_service.set_overtime_config(db, admin.tenant_id, body)


class ResettleMonthRequest(BaseModel):
    year: int
    month: int
    employee_id: uuid.UUID | None = None


class ResettleMonthResult(BaseModel):
    employee_id: uuid.UUID
    employee_number: str | None
    alte_auszahlung: float
    neue_auszahlung: float


@router.post("/overtime/resettle", response_model=list[ResettleMonthResult])
async def resettle_overtime_month(
    body: ResettleMonthRequest, db: DBSession, admin: AuthUser
) -> list[ResettleMonthResult]:
    """Monatsabschluss zurücksetzen und neu ausführen (nach nachträglichen Korrekturen).

    Löscht die accrual-/payout-Zeilen samt UEBERSTUNDEN_AUSZAHLUNG-Lohnbuchung
    dieses Monats und lässt ``settle_month`` frisch laufen — nötig, wenn nach
    einem bereits gelaufenen Abschluss noch ein Anfangsbestand oder eine
    Korrektur nachgebucht wurde (die Sperre in ``settle_month`` verhindert
    sonst die Neuberechnung). Ohne ``employee_id`` werden alle aktiven
    Mitarbeiter des Mandanten neu abgeschlossen. Nur vergangene Monate sind
    zulässig. Requires ``admin:settings`` oder ``overtime:manage``.
    """
    if not (admin.has_permission("admin:settings") or admin.has_permission("overtime:manage")):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin:settings oder overtime:manage erforderlich",
        )
    results = await payroll_config_service.resettle_overtime_month(
        db,
        admin.tenant_id,
        year=body.year,
        month=body.month,
        employee_id=body.employee_id,
        actor_employee_id=admin.employee_id,
    )
    return [ResettleMonthResult(**r) for r in results]


@router.get("/wage-profiles")
async def list_wage_profiles(db: DBSession, admin: AuthUser) -> dict:
    """Abrechnungsprofile (Name + abweichende Kategorie-Zuordnungen)."""
    if not admin.has_permission("admin:settings"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="admin:settings erforderlich"
        )
    return await payroll_config_service.list_wage_profiles(db, admin.tenant_id)


@router.patch("/wage-profiles")
async def upsert_wage_profile(body: dict, db: DBSession, admin: AuthUser) -> dict:
    """Profil anlegen/aendern: {id?, name, mapping, fruehester_beginn?,
    mehrarbeit_frei_wochen_std?, wochenende_ins_konto?}

    (Regeln sind optional; siehe unten.)

    mapping enthaelt NUR die Abweichungen vom Standard; null/fehlend = Standard.
    ``fruehester_beginn`` ("HH:MM"), ``mehrarbeit_frei_wochen_std`` (Std/Woche) und
    ``wochenende_ins_konto`` (bool) sind zusaetzliche Profil-Regeln (nur fuer
    Nicht-Standard-Profile).
    """
    if not admin.has_permission("admin:settings"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="admin:settings erforderlich"
        )
    return await payroll_config_service.upsert_wage_profile(db, admin.tenant_id, body)


@router.delete("/wage-profiles/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_wage_profile(profile_id: str, db: DBSession, admin: AuthUser) -> None:
    """Profil loeschen; zugewiesene Mitarbeiter fallen auf den Standard zurueck."""
    if not admin.has_permission("admin:settings"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="admin:settings erforderlich"
        )
    await payroll_config_service.delete_wage_profile(db, admin.tenant_id, profile_id)
