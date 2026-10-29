"""Payroll/Lohnarten configuration: wage-type mappings, surcharge (Zuschlag)
config, category->wage-type assignments, overtime rules, monthly resettle,
and per-group wage profiles (Abrechnungsprofile).

Moved verbatim out of the former monolithic ``api/v1/admin.py``
(zeiterfassung-6pcv).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from zeiterfassung.db.scoping import tenant_scoped
from zeiterfassung.models.export import WageTypeMapping
from zeiterfassung.services.tenant_service import get_tenant_or_404

# ---------------------------------------------------------------------------
# Wage-type mappings (internal code -> DATEV Lohnart)
# ---------------------------------------------------------------------------


async def list_wage_types(db: AsyncSession, tenant_id: uuid.UUID) -> list[dict]:
    """List wage-type mappings for the tenant."""
    from zeiterfassung.payroll.categories import get_category

    result = await db.execute(
        tenant_scoped(select(WageTypeMapping), WageTypeMapping, tenant_id).order_by(
            WageTypeMapping.datev_lohnart
        )
    )
    rows = result.scalars().all()

    def _label(code: str) -> str | None:
        try:
            return get_category(code).label
        except KeyError:
            return None

    # Nur der echte Katalog (aus dem Steuerberater-PDF gepflegt) — die
    # System-Kategorien werden separat ueber die ZUORDNUNG verknuepft.
    return [
        {
            "internal_code": r.internal_code,
            "datev_lohnart": r.datev_lohnart,
            "description": r.description or _label(r.internal_code),
            "is_active": r.is_active,
            "unit": r.unit,
            "folge_lohnart": r.folge_lohnart,
        }
        for r in rows
    ]


async def upsert_wage_type(db: AsyncSession, tenant_id: uuid.UUID, body: dict) -> dict:
    """Create or update a wage-type mapping by (tenant_id, internal_code)."""
    internal_code = body.get("internal_code")
    if not internal_code:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="internal_code is required",
        )
    # Nummer ist OPTIONAL (leer = noch nicht vergeben -> Kategorie wird nicht exportiert).
    datev_lohnart = body.get("datev_lohnart")
    if datev_lohnart is None or str(datev_lohnart).strip() == "":
        datev_lohnart = None
    else:
        try:
            datev_lohnart = int(str(datev_lohnart).strip())
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="datev_lohnart muss eine Zahl sein",
            ) from None
    description = body.get("description")
    is_active = body.get("is_active", True)
    unit = str(body.get("unit") or "hours").strip().lower()
    if unit not in ("hours", "amount", "days"):
        unit = "hours"
    folge_lohnart = (body.get("folge_lohnart") or "").strip() or None
    if folge_lohnart == internal_code:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Folgelohnart darf nicht auf sich selbst zeigen",
        )

    result = await db.execute(
        tenant_scoped(select(WageTypeMapping), WageTypeMapping, tenant_id).where(
            WageTypeMapping.internal_code == internal_code,
        )
    )
    row = result.scalar_one_or_none()
    if row is not None:
        row.datev_lohnart = datev_lohnart
        row.description = description
        row.is_active = is_active
        row.unit = unit
        row.folge_lohnart = folge_lohnart
    else:
        db.add(
            WageTypeMapping(
                tenant_id=tenant_id,
                internal_code=internal_code,
                datev_lohnart=datev_lohnart,
                description=description,
                is_active=is_active,
                unit=unit,
                folge_lohnart=folge_lohnart,
                created_at=datetime.now(UTC),
            )
        )

    await db.commit()
    return {"ok": True}


async def delete_wage_type(db: AsyncSession, tenant_id: uuid.UUID, internal_code: str) -> None:
    """Delete a wage-type mapping (by internal_code)."""
    row = (
        await db.execute(
            tenant_scoped(select(WageTypeMapping), WageTypeMapping, tenant_id).where(
                WageTypeMapping.internal_code == internal_code,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Wage type not found")
    await db.delete(row)

    # Verwaiste Zuordnungen mit aufraeumen: Kategorien, die auf diese Lohnart
    # zeigten, springen zurueck auf "nicht zugeordnet" — sowohl in der
    # Standard-Zuordnung als auch in allen Abrechnungsprofilen. Sonst blieben
    # tote Verweise in tenant.settings stehen (leere Dropdowns im Profil-Editor).
    from zeiterfassung.models.tenant import Tenant

    tenant = (await db.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one_or_none()
    if tenant is not None:
        settings_ = dict(tenant.settings or {})
        zuordnung = {
            cat: code
            for cat, code in (settings_.get("lohnart_zuordnung") or {}).items()
            if code != internal_code
        }
        settings_["lohnart_zuordnung"] = zuordnung
        profiles = []
        for p in settings_.get("wage_profiles") or []:
            p = dict(p)
            p["mapping"] = {
                cat: code for cat, code in (p.get("mapping") or {}).items() if code != internal_code
            }
            profiles.append(p)
        settings_["wage_profiles"] = profiles
        tenant.settings = settings_

    await db.commit()


async def seed_default_wage_types(db: AsyncSession, tenant_id: uuid.UUID) -> dict:
    """Standard-Kategorien (ohne Nummern) in die DB uebernehmen.

    Only missing ``internal_code`` rows are created; existing rows are left
    untouched. Returns the number of rows created.
    """
    from zeiterfassung.payroll.categories import all_categories

    result = await db.execute(
        tenant_scoped(select(WageTypeMapping.internal_code), WageTypeMapping, tenant_id)
    )
    existing = {code for code in result.scalars().all()}

    created = 0
    now = datetime.now(UTC)
    for cat in all_categories():
        code, lohnart = cat.code, None
        if code in existing:
            continue
        db.add(
            WageTypeMapping(
                tenant_id=tenant_id,
                internal_code=code,
                datev_lohnart=lohnart,
                description=None,
                is_active=True,
                created_at=now,
            )
        )
        created += 1

    await db.commit()
    return {"created": created}


# ---------------------------------------------------------------------------
# Surcharge (Zuschlag) config
# ---------------------------------------------------------------------------

# Default surcharge configuration used when a tenant has no "zuschlaege" settings.
_SURCHARGE_DEFAULTS: dict[str, float] = {
    "night_start": 23,
    "night_end": 6,
    "night_pct": 25,
    "sunday_pct": 50,
    "holiday_pct": 125,
    "saturday_pct": 0,
    "saturday_from": 0,
    "sunday_from": 0,
    # Export-Basis: "ist" (gearbeitete Stunden) oder "soll" (LODAS-kompatibel
    # wie das Altsystem: Zeitlohn = effektive Sollzeit + Mehrarbeit-Staffel).
    "mehrarbeit_frei_wochen_std": 1.5,
}


async def get_surcharge_config(db: AsyncSession, tenant_id: uuid.UUID) -> dict:
    """Return the tenant's surcharge configuration (Zuschlagssaetze).

    Missing fields are filled with defaults (night_start=23, night_end=6,
    night_pct=25, sunday_pct=50, holiday_pct=125, saturday_pct=0).
    """
    tenant = await get_tenant_or_404(db, tenant_id)
    z = (tenant.settings or {}).get("zuschlaege") or {}
    return {
        "night_start": int(z.get("night_start", _SURCHARGE_DEFAULTS["night_start"])),
        "night_end": int(z.get("night_end", _SURCHARGE_DEFAULTS["night_end"])),
        "night_pct": float(z.get("night_pct", _SURCHARGE_DEFAULTS["night_pct"])),
        "sunday_pct": float(z.get("sunday_pct", _SURCHARGE_DEFAULTS["sunday_pct"])),
        "holiday_pct": float(z.get("holiday_pct", _SURCHARGE_DEFAULTS["holiday_pct"])),
        "saturday_pct": float(z.get("saturday_pct", _SURCHARGE_DEFAULTS["saturday_pct"])),
        "saturday_from": int(z.get("saturday_from", 0)),
        "sunday_from": int(z.get("sunday_from", 0)),
        "export_basis": str(z.get("export_basis", "ist")),
        "mehrarbeit_frei_wochen_std": float(
            z.get("mehrarbeit_frei_wochen_std", _SURCHARGE_DEFAULTS["mehrarbeit_frei_wochen_std"])
        ),
    }


async def set_surcharge_config(db: AsyncSession, tenant_id: uuid.UUID, body: dict) -> dict:
    """Persist the tenant's surcharge configuration into ``Tenant.settings["zuschlaege"]``.

    Tolerant of missing fields (filled with defaults) and casts hour fields to
    int and percentage fields to float.
    """
    tenant = await get_tenant_or_404(db, tenant_id)

    body = body or {}
    prev = (tenant.settings or {}).get("zuschlaege") or {}

    def _cur(key):
        return prev.get(key, _SURCHARGE_DEFAULTS[key])

    zuschlaege = {
        "night_start": int(body.get("night_start", _cur("night_start"))),
        "night_end": int(body.get("night_end", _cur("night_end"))),
        "night_pct": float(body.get("night_pct", _cur("night_pct"))),
        "sunday_pct": float(body.get("sunday_pct", _cur("sunday_pct"))),
        "holiday_pct": float(body.get("holiday_pct", _cur("holiday_pct"))),
        "saturday_pct": float(body.get("saturday_pct", _cur("saturday_pct"))),
        "saturday_from": int(body.get("saturday_from", _cur("saturday_from"))),
        "sunday_from": int(body.get("sunday_from", _cur("sunday_from"))),
        "mehrarbeit_frei_wochen_std": float(
            body.get("mehrarbeit_frei_wochen_std", _cur("mehrarbeit_frei_wochen_std"))
        ),
    }
    basis = str(body.get("export_basis", prev.get("export_basis", "ist")))
    zuschlaege["export_basis"] = basis if basis in ("ist", "soll") else "ist"

    settings = dict(tenant.settings or {})
    settings["zuschlaege"] = zuschlaege
    tenant.settings = settings
    await db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Category -> wage-type assignments (Lohnart-Zuordnung)
# ---------------------------------------------------------------------------


async def get_wage_type_assignments(db: AsyncSession, tenant_id: uuid.UUID) -> dict:
    """Zuordnung System-Kategorien -> Katalog-Lohnart (aus dem Steuerberater-PDF).

    Der Export berechnet die Kategorien automatisch und bucht sie auf die hier
    zugeordnete Lohnart. Ohne Zuordnung wird eine Kategorie nicht exportiert.
    """
    from zeiterfassung.models.tenant import Tenant
    from zeiterfassung.payroll.categories import all_categories

    tenant = (await db.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one_or_none()
    assigned = ((tenant.settings or {}).get("lohnart_zuordnung") or {}) if tenant else {}

    catalog = (
        (
            await db.execute(
                tenant_scoped(select(WageTypeMapping), WageTypeMapping, tenant_id)
                .where(
                    WageTypeMapping.is_active == True,  # noqa: E712
                    WageTypeMapping.datev_lohnart.is_not(None),
                )
                .order_by(WageTypeMapping.datev_lohnart)
            )
        )
        .scalars()
        .all()
    )
    valid_codes = {c.internal_code for c in catalog}

    return {
        "categories": [
            {
                "code": cat.code,
                "label": cat.label,
                "unit": cat.unit,
                "assigned": assigned.get(cat.code)
                if assigned.get(cat.code) in valid_codes
                else None,
            }
            for cat in all_categories()
        ],
        "options": [
            {
                "internal_code": c.internal_code,
                "datev_lohnart": c.datev_lohnart,
                "description": c.description,
            }
            for c in catalog
        ],
    }


async def set_wage_type_assignments(db: AsyncSession, tenant_id: uuid.UUID, body: dict) -> dict:
    """Zuordnungen speichern: Body {assignments: {CATEGORY_CODE: internal_code|null}}."""
    from zeiterfassung.models.tenant import Tenant
    from zeiterfassung.payroll.categories import all_categories

    updates = (body or {}).get("assignments") or {}
    cat_codes = {c.code for c in all_categories()}
    valid_codes = set(
        (
            await db.execute(
                tenant_scoped(
                    select(WageTypeMapping.internal_code), WageTypeMapping, tenant_id
                ).where(
                    WageTypeMapping.is_active == True,  # noqa: E712
                )
            )
        )
        .scalars()
        .all()
    )

    tenant = (await db.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one_or_none()
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    settings = dict(tenant.settings or {})
    zuordnung = dict(settings.get("lohnart_zuordnung") or {})

    for cat, code in updates.items():
        if cat not in cat_codes:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Unbekannte Kategorie: {cat}",
            )
        if code is None or code == "":
            zuordnung.pop(cat, None)
        else:
            if code not in valid_codes:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Unbekannte Lohnart: {code}",
                )
            zuordnung[cat] = code

    settings["lohnart_zuordnung"] = zuordnung
    tenant.settings = settings
    await db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Overtime rules (Ueberstundenregeln) + resettle
# ---------------------------------------------------------------------------

#: Defaults fuer die Ueberstundenregeln (Tenant.settings["ueberstunden"]).
_OVERTIME_DEFAULTS = {
    "max_balance_hours": 120.0,
    "expiry_months": 12,
    "allow_payout": True,
    "allow_compensation": True,
}


async def get_overtime_config(db: AsyncSession, tenant_id: uuid.UUID) -> dict:
    """Return the tenant's overtime rules (Ueberstundenregeln).

    Missing fields are filled with defaults (max_balance_hours=120,
    expiry_months=12, allow_payout=True, allow_compensation=True).
    """
    tenant = await get_tenant_or_404(db, tenant_id)
    o = (tenant.settings or {}).get("ueberstunden") or {}
    return {
        "max_balance_hours": float(
            o.get("max_balance_hours", _OVERTIME_DEFAULTS["max_balance_hours"])
        ),
        "expiry_months": int(o.get("expiry_months", _OVERTIME_DEFAULTS["expiry_months"])),
        "allow_payout": bool(o.get("allow_payout", _OVERTIME_DEFAULTS["allow_payout"])),
        "allow_compensation": bool(
            o.get("allow_compensation", _OVERTIME_DEFAULTS["allow_compensation"])
        ),
    }


async def set_overtime_config(db: AsyncSession, tenant_id: uuid.UUID, body: dict) -> dict:
    """Persist the tenant's overtime rules into ``Tenant.settings["ueberstunden"]``.

    Tolerant of missing fields (filled with defaults) and casts
    max_balance_hours to float, expiry_months to int and the flags to bool.
    """
    tenant = await get_tenant_or_404(db, tenant_id)

    body = body or {}
    ueberstunden = {
        "max_balance_hours": float(
            body.get("max_balance_hours", _OVERTIME_DEFAULTS["max_balance_hours"])
        ),
        "expiry_months": int(body.get("expiry_months", _OVERTIME_DEFAULTS["expiry_months"])),
        "allow_payout": bool(body.get("allow_payout", _OVERTIME_DEFAULTS["allow_payout"])),
        "allow_compensation": bool(
            body.get("allow_compensation", _OVERTIME_DEFAULTS["allow_compensation"])
        ),
    }

    settings = dict(tenant.settings or {})
    settings["ueberstunden"] = ueberstunden
    tenant.settings = settings
    await db.commit()
    return {"ok": True}


async def resettle_overtime_month(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    year: int,
    month: int,
    employee_id: uuid.UUID | None,
    actor_employee_id: uuid.UUID | None,
) -> list[dict]:
    """Monatsabschluss zurücksetzen und neu ausführen (nach nachträglichen Korrekturen).

    Löscht die accrual-/payout-Zeilen samt UEBERSTUNDEN_AUSZAHLUNG-Lohnbuchung
    dieses Monats und lässt ``settle_month`` frisch laufen — nötig, wenn nach
    einem bereits gelaufenen Abschluss noch ein Anfangsbestand oder eine
    Korrektur nachgebucht wurde (die Sperre in ``settle_month`` verhindert
    sonst die Neuberechnung). Ohne ``employee_id`` werden alle aktiven
    Mitarbeiter des Mandanten neu abgeschlossen. Nur vergangene Monate sind
    zulässig.
    """
    from zeiterfassung.models.user import User
    from zeiterfassung.services import overtime_service

    today = datetime.now(tz=UTC).date()
    current_ym = date(today.year, today.month, 1)
    try:
        target_ym = date(year, month, 1)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Ungültiger Monat"
        ) from exc
    if target_ym >= current_ym:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Der laufende Monat rechnet automatisch mit — neu berechnen geht ab "
                "dem 1. des Folgemonats."
            ),
        )

    if employee_id is not None:
        users = list(
            (
                await db.execute(
                    tenant_scoped(select(User), User, tenant_id).where(User.id == employee_id)
                )
            )
            .scalars()
            .all()
        )
        if not users:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Mitarbeiter nicht gefunden"
            )
    else:
        users = list(
            (
                await db.execute(
                    tenant_scoped(select(User), User, tenant_id).where(
                        User.is_active == True,  # noqa: E712
                    )
                )
            )
            .scalars()
            .all()
        )

    results: list[dict] = []
    for u in users:
        r = await overtime_service.resettle_month(db, tenant_id, u, year, month, actor_employee_id)
        results.append(
            {
                "employee_id": u.id,
                "employee_number": u.employee_number,
                "alte_auszahlung": float(r["alte_auszahlung"]),
                "neue_auszahlung": float(r["neue_auszahlung"]),
            }
        )
    await db.commit()
    return results


# ---------------------------------------------------------------------------
# Abrechnungsprofile: abweichende Lohnart-Zuordnungen pro Mitarbeiter-Gruppe
# ---------------------------------------------------------------------------


async def _valid_la_codes(db: AsyncSession, tenant_id: uuid.UUID) -> set[str]:
    rows = (
        await db.execute(
            tenant_scoped(select(WageTypeMapping.internal_code), WageTypeMapping, tenant_id).where(
                WageTypeMapping.is_active == True,  # noqa: E712
                WageTypeMapping.datev_lohnart.is_not(None),
            )
        )
    ).all()
    return {r[0] for r in rows}


async def list_wage_profiles(db: AsyncSession, tenant_id: uuid.UUID) -> dict:
    """Abrechnungsprofile (Name + abweichende Kategorie-Zuordnungen)."""
    tenant = await get_tenant_or_404(db, tenant_id)
    settings = tenant.settings or {}
    profiles = list(settings.get("wage_profiles") or [])
    from sqlalchemy import func

    from zeiterfassung.models.user import User

    # Verwaiste Verweise (Lohnart inzwischen geloescht/inaktiv) ausblenden —
    # die Kategorie erscheint dann als "nicht zugeordnet" statt als leeres Feld.
    valid_codes = await _valid_la_codes(db, tenant_id)

    def _clean(mapping: dict) -> dict:
        return {cat: code for cat, code in (mapping or {}).items() if code in valid_codes}

    counts = {
        r[0]: r[1]
        for r in (
            await db.execute(
                tenant_scoped(select(User.wage_profile_id, func.count()), User, tenant_id)
                .where(User.is_active == True)  # noqa: E712
                .group_by(User.wage_profile_id)
            )
        ).all()
    }
    for p in profiles:
        p["mapping"] = _clean(p.get("mapping"))
        p["employee_count"] = counts.get(p.get("id"), 0)
        p["is_standard"] = False
    # Der bisherige globale Standard (lohnart_zuordnung) IST das Profil
    # "Standard": gilt fuer alle Mitarbeiter ohne eigenes Profil, nicht loeschbar.
    standard = {
        "id": "standard",
        "name": settings.get("standard_profile_name") or "Standard",
        "mapping": _clean(settings.get("lohnart_zuordnung")),
        "employee_count": counts.get(None, 0),
        "is_standard": True,
    }
    return {"profiles": [standard] + profiles}


async def upsert_wage_profile(db: AsyncSession, tenant_id: uuid.UUID, body: dict) -> dict:
    """Profil anlegen/aendern: {id?, name, mapping, fruehester_beginn?,
    mehrarbeit_frei_wochen_std?, wochenende_ins_konto?}

    (Regeln sind optional; siehe unten.)

    mapping enthaelt NUR die Abweichungen vom Standard; null/fehlend = Standard.
    ``fruehester_beginn`` ("HH:MM"), ``mehrarbeit_frei_wochen_std`` (Std/Woche) und
    ``wochenende_ins_konto`` (bool) sind zusaetzliche Profil-Regeln (nur fuer
    Nicht-Standard-Profile).
    """
    import re as _re
    import uuid as _uuid

    from zeiterfassung.payroll.categories import all_categories

    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Name erforderlich"
        )
    valid_categories = {c.code for c in all_categories()}
    valid_codes = await _valid_la_codes(db, tenant_id)
    mapping: dict = {}
    for category, code in (body.get("mapping") or {}).items():
        if category not in valid_categories:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Unbekannte Kategorie: {category}",
            )
        if code:
            if code not in valid_codes:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Unbekannte/inaktive Lohnart: {code}",
                )
            mapping[category] = code

    profile_id = body.get("id") or str(_uuid.uuid4())[:8]

    # Profil-Regeln: nur fuer Nicht-Standard-Profile — das Standard-Profil
    # nutzt die Mandanten-Einstellungen (Einstellungen -> Zuschlaege).
    fruehester_beginn = body.get("fruehester_beginn") or None
    if fruehester_beginn is not None:
        if profile_id == "standard":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "Fuer das Standard-Profil gilt die Mandanten-Einstellung "
                    "(Einstellungen -> Zuschlaege)."
                ),
            )
        if not _re.fullmatch(r"\d{1,2}:\d{2}", fruehester_beginn):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Fruehester Beginn muss im Format HH:MM sein.",
            )
        hh, mm = fruehester_beginn.split(":")
        if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Fruehester Beginn muss zwischen 00:00 und 23:59 liegen.",
            )

    mehrarbeit_frei_raw = body.get("mehrarbeit_frei_wochen_std")
    mehrarbeit_frei: float | None = None
    if mehrarbeit_frei_raw is not None and mehrarbeit_frei_raw != "":
        if profile_id == "standard":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "Fuer das Standard-Profil gilt die Mandanten-Einstellung "
                    "(Einstellungen -> Zuschlaege)."
                ),
            )
        try:
            mehrarbeit_frei = float(mehrarbeit_frei_raw)
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Mehrarbeit-Freikontingent muss eine Zahl sein.",
            ) from exc
        if mehrarbeit_frei < 0:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Mehrarbeit-Freikontingent darf nicht negativ sein.",
            )

    wochenende_ins_konto = body.get("wochenende_ins_konto")
    if wochenende_ins_konto is not None and profile_id == "standard":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Fuer das Standard-Profil gilt die Mandanten-Einstellung "
                "(Einstellungen -> Zuschlaege)."
            ),
        )

    tenant = await get_tenant_or_404(db, tenant_id)
    settings = dict(tenant.settings or {})
    if profile_id == "standard":
        # Standard-Profil = die globale Zuordnung (gilt ohne eigenes Profil).
        settings["lohnart_zuordnung"] = mapping
        settings["standard_profile_name"] = name
    else:
        profiles = list(settings.get("wage_profiles") or [])
        profiles = [p for p in profiles if p.get("id") != profile_id]
        profile: dict = {"id": profile_id, "name": name, "mapping": mapping}
        if fruehester_beginn:
            profile["fruehester_beginn"] = fruehester_beginn
        if mehrarbeit_frei is not None:
            profile["mehrarbeit_frei_wochen_std"] = mehrarbeit_frei
        if wochenende_ins_konto is not None:
            profile["wochenende_ins_konto"] = bool(wochenende_ins_konto)
        profiles.append(profile)
        profiles.sort(key=lambda p: str(p.get("name", "")).lower())
        settings["wage_profiles"] = profiles
    tenant.settings = settings
    await db.commit()
    return {"ok": True, "id": profile_id}


async def delete_wage_profile(db: AsyncSession, tenant_id: uuid.UUID, profile_id: str) -> None:
    """Profil loeschen; zugewiesene Mitarbeiter fallen auf den Standard zurueck."""
    if profile_id == "standard":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Das Standard-Profil kann nicht geloescht werden.",
        )
    from sqlalchemy import update

    from zeiterfassung.models.user import User

    tenant = await get_tenant_or_404(db, tenant_id)
    settings = dict(tenant.settings or {})
    settings["wage_profiles"] = [
        p for p in (settings.get("wage_profiles") or []) if p.get("id") != profile_id
    ]
    tenant.settings = settings
    await db.execute(
        update(User)
        .where(User.tenant_id == tenant_id, User.wage_profile_id == profile_id)
        .values(wage_profile_id=None)
    )
    await db.commit()
