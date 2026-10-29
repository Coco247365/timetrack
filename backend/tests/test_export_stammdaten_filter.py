"""DATEV-Export: Stammdaten-Haertung (LODAS-Abgleich 11.08.2026).

1. Mitarbeiter OHNE Personalnummer erzeugen keine Zeile mehr (frueher landete
   ein UUID-Fragment wie "2adfd25e" in der Datei — DATEV kann das nicht zuordnen).
2. Mitarbeiter mit Eintritt NACH dem Exportmonat tauchen nicht auf (ein
   ein Mitarbeiter stand mit 184 h im Juli-Export trotz Eintritt 01.08.).
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy.orm.attributes import flag_modified

from zeiterfassung.core.security import hash_password
from zeiterfassung.models.clock import DailyTimesheet
from zeiterfassung.models.user import User
from zeiterfassung.services.export_service import _build_datev_rows

WAGE_TYPES = {"NORMALE_ARBEITSZEIT": 11}


async def _soll_basis(db, tenant):
    s = dict(tenant.settings or {})
    s["zuschlaege"] = {"export_basis": "soll"}
    tenant.settings = s
    flag_modified(tenant, "settings")
    await db.flush()


async def _mk_user(db, tenant, *, pnr, hire, email):
    now = datetime.now(tz=UTC)
    u = User(
        tenant_id=tenant.id,
        email=email,
        hashed_password=hash_password("Passw0rd!"),
        first_name="T",
        last_name=email.split("@")[0],
        employee_number=pnr,
        contracted_hours_per_week=40.0,
        employment_type="hourly",
        hire_date=hire,
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    db.add(u)
    await db.flush()
    return u


async def _mk_day(db, tenant, u, d, minutes=480):
    db.add(
        DailyTimesheet(
            tenant_id=tenant.id,
            employee_id=u.id,
            work_date=d,
            net_work_minutes=minutes,
            gross_work_minutes=minutes,
            break_minutes=0,
            status="COMPLETE",
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
    )
    await db.flush()


@pytest.mark.asyncio
async def test_ohne_personalnummer_keine_zeile(db, tenant):
    await _soll_basis(db, tenant)
    u = await _mk_user(db, tenant, pnr=None, hire=date(2026, 1, 1), email="ohne@test.de")
    await _mk_day(db, tenant, u, date(2026, 7, 6))
    await db.commit()
    rows = await _build_datev_rows(db, tenant.id, 2026, 7, [u.id], WAGE_TYPES)
    assert rows == []  # kein UUID-Fragment als Personalnummer


@pytest.mark.asyncio
async def test_eintritt_nach_exportmonat_keine_zeile(db, tenant):
    await _soll_basis(db, tenant)
    u = await _mk_user(db, tenant, pnr="104", hire=date(2026, 8, 1), email="neu@test.de")
    # Scheindaten im Juli (z. B. von Auto-Erfassung vor Korrektur des Eintritts)
    await _mk_day(db, tenant, u, date(2026, 7, 6))
    await db.commit()
    rows = await _build_datev_rows(db, tenant.id, 2026, 7, [u.id], WAGE_TYPES)
    assert rows == []


@pytest.mark.asyncio
async def test_mit_pnr_und_fruehem_eintritt_bleibt(db, tenant):
    await _soll_basis(db, tenant)
    u = await _mk_user(db, tenant, pnr="42", hire=date(2026, 1, 1), email="ok@test.de")
    await _mk_day(db, tenant, u, date(2026, 7, 6))
    await db.commit()
    rows = await _build_datev_rows(db, tenant.id, 2026, 7, [u.id], WAGE_TYPES)
    assert any(r.personalnummer == "42" for r in rows)


@pytest.mark.asyncio
async def test_geloeschte_nie_im_export(db, tenant):
    await _soll_basis(db, tenant)
    u = await _mk_user(db, tenant, pnr="9001", hire=date(2026, 1, 1), email="del@test.de")
    u.employment_status = "deleted"
    await _mk_day(db, tenant, u, date(2026, 7, 6))
    await db.commit()
    rows = await _build_datev_rows(db, tenant.id, 2026, 7, [u.id], WAGE_TYPES)
    assert rows == []


@pytest.mark.asyncio
async def test_austritt_vor_monat_raus_im_monat_drin(db, tenant):
    await _soll_basis(db, tenant)
    # Austritt im Juni -> Juli-Export ohne ihn
    alt = await _mk_user(db, tenant, pnr="9002", hire=date(2026, 1, 1), email="alt@test.de")
    alt.employment_status = "archived"
    alt.termination_date = date(2026, 6, 15)
    await _mk_day(db, tenant, alt, date(2026, 7, 6))
    # Austritt Mitte Juli -> Schlussmonat Juli MUSS drin sein
    juli = await _mk_user(db, tenant, pnr="9003", hire=date(2026, 1, 1), email="juli@test.de")
    juli.employment_status = "archived"
    juli.termination_date = date(2026, 7, 15)
    await _mk_day(db, tenant, juli, date(2026, 7, 6))
    await db.commit()
    rows = await _build_datev_rows(db, tenant.id, 2026, 7, [alt.id, juli.id], WAGE_TYPES)
    pnrs = {r.personalnummer for r in rows}
    assert "9002" not in pnrs
    assert "9003" in pnrs


@pytest.mark.asyncio
async def test_werkstudent_nur_ist_stunden_auf_zeitlohn(db, tenant):
    """Werkstudenten/Aushilfen: genau eine Zeile Zeitlohn mit den Ist-Stunden
    (Entscheidung 09.09.2026) — kein Soll, keine Staffel, keine Minusstunden."""
    await _soll_basis(db, tenant)
    u = await _mk_user(db, tenant, pnr="22028", hire=date(2026, 1, 1), email="ws@test.de")
    u.employment_type = "werkstudent"
    await _mk_day(db, tenant, u, date(2026, 7, 6))
    await _mk_day(db, tenant, u, date(2026, 7, 7), minutes=270)
    await db.commit()
    rows = await _build_datev_rows(db, tenant.id, 2026, 7, [u.id], WAGE_TYPES)
    assert [(r.lohnart, r.wert) for r in rows] == [(11, 12.5)]
    # Auch im Mitarbeiterkreis "Nur Lohn-Empfaenger" (Standard der Export-Seite)
    # bleibt die Werkstudentin drin — sie wird nach Stunden bezahlt.
    rows = await _build_datev_rows(
        db, tenant.id, 2026, 7, [u.id], WAGE_TYPES, employment_type="hourly"
    )
    assert [(r.lohnart, r.wert) for r in rows] == [(11, 12.5)]
