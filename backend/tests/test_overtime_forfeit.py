"""Auszahl-Modus ``forfeit_above_cap``: Überlauf über der Grenze VERFÄLLT.

Altsystem deckelt das Gleitzeitkonto bei 20 h und lässt den Rest ersatzlos
verfallen — anders als unser ``above_cap``, das ihn auszahlt. Der Unterschied
ist bares Geld, deshalb ein eigener Modus statt einer Einstellung „nicht
auszahlen".

Der Verfall wird als sichtbare Ledger-Zeile gebucht (``entry_type='forfeit'``),
nicht als stille Kappung: sonst ist „wo sind meine drei Stunden hin?" nicht zu
beantworten (BetrVG §87 Abs. 1 Nr. 6).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select

from zeiterfassung.models.overtime import OvertimeEntry
from zeiterfassung.models.payroll_booking import PayrollBooking
from zeiterfassung.services import overtime_service

_YM = date(2026, 6, 1)


async def _entries(db, emp, ym=_YM) -> list[OvertimeEntry]:
    return list(
        (
            await db.execute(
                select(OvertimeEntry).where(
                    OvertimeEntry.employee_id == emp.id, OvertimeEntry.year_month == ym
                )
            )
        )
        .scalars()
        .all()
    )


async def _anfangsbestand(db, tenant, employee, stunden: str) -> None:
    """Bestand aufs Konto legen, ohne Tagesbögen zu bauen."""
    db.add(
        OvertimeEntry(
            tenant_id=tenant.id,
            employee_id=employee.id,
            year_month=_YM,
            entry_date=date(2026, 6, 1),
            entry_type="adjustment",
            hours=Decimal(stunden),
            reason="Anfangsbestand (Test)",
            created_at=datetime.now(tz=UTC),
        )
    )
    # Auto-Erfassung: leere Tage erzeugen sonst Fehlzeiten-Minus und
    # überlagern den Bestand, um den es hier geht.
    employee.time_capture = "auto"
    employee.overtime_cap_hours = 20.0
    await db.flush()


class TestVerfallUeberCap:
    async def test_ueberlauf_verfaellt_und_wird_gebucht(self, db, tenant, employee):
        employee.overtime_payout_mode = "forfeit_above_cap"
        await _anfangsbestand(db, tenant, employee, "26")

        await overtime_service.settle_month(db, tenant.id, employee, 2026, 6, None)

        verfall = [e for e in await _entries(db, employee) if e.entry_type == "forfeit"]
        assert len(verfall) == 1
        assert Decimal(str(verfall[0].hours)) == Decimal("-6.00")
        assert "verfallen" in (verfall[0].reason or "").lower()

    async def test_saldo_endet_exakt_auf_der_grenze(self, db, tenant, employee):
        employee.overtime_payout_mode = "forfeit_above_cap"
        await _anfangsbestand(db, tenant, employee, "26")

        await overtime_service.settle_month(db, tenant.id, employee, 2026, 6, None)

        saldo = await overtime_service.balance_for_employee(db, tenant.id, employee.id)
        assert Decimal(str(saldo["balance_hours"])) == Decimal("20.00")

    async def test_keine_lohnbuchung(self, db, tenant, employee):
        """Der Kern: verfallen heißt NICHT auszahlen — nichts darf in den Lohn."""
        employee.overtime_payout_mode = "forfeit_above_cap"
        await _anfangsbestand(db, tenant, employee, "26")

        await overtime_service.settle_month(db, tenant.id, employee, 2026, 6, None)

        buchungen = (
            (
                await db.execute(
                    select(PayrollBooking).where(PayrollBooking.employee_id == employee.id)
                )
            )
            .scalars()
            .all()
        )
        assert buchungen == []
        assert not [e for e in await _entries(db, employee) if e.entry_type == "payout"]

    async def test_unter_der_grenze_verfaellt_nichts(self, db, tenant, employee):
        employee.overtime_payout_mode = "forfeit_above_cap"
        await _anfangsbestand(db, tenant, employee, "12")

        await overtime_service.settle_month(db, tenant.id, employee, 2026, 6, None)

        assert not [e for e in await _entries(db, employee) if e.entry_type == "forfeit"]
        saldo = await overtime_service.balance_for_employee(db, tenant.id, employee.id)
        assert Decimal(str(saldo["balance_hours"])) == Decimal("12.00")

    async def test_minus_bleibt_unangetastet(self, db, tenant, employee):
        """Nach unten gibt es keinen Deckel — Minus ist eine Schuld, kein Verfall."""
        employee.overtime_payout_mode = "forfeit_above_cap"
        await _anfangsbestand(db, tenant, employee, "-15")

        await overtime_service.settle_month(db, tenant.id, employee, 2026, 6, None)

        assert not [e for e in await _entries(db, employee) if e.entry_type == "forfeit"]
        saldo = await overtime_service.balance_for_employee(db, tenant.id, employee.id)
        assert Decimal(str(saldo["balance_hours"])) == Decimal("-15.00")

    async def test_zweiter_abschluss_verfaellt_nicht_erneut(self, db, tenant, employee):
        employee.overtime_payout_mode = "forfeit_above_cap"
        await _anfangsbestand(db, tenant, employee, "26")

        await overtime_service.settle_month(db, tenant.id, employee, 2026, 6, None)
        await overtime_service.settle_month(db, tenant.id, employee, 2026, 6, None)

        assert len([e for e in await _entries(db, employee) if e.entry_type == "forfeit"]) == 1

    async def test_verfall_taucht_im_verlauf_auf(self, db, tenant, employee):
        """Sichtbarkeit: die Zeile muss im Tagesverlauf des Kontos stehen."""
        employee.overtime_payout_mode = "forfeit_above_cap"
        await _anfangsbestand(db, tenant, employee, "26")
        await overtime_service.settle_month(db, tenant.id, employee, 2026, 6, None)

        rows = await overtime_service.daily_for_employee(db, tenant.id, employee.id)
        verfall = [r for r in rows if r.get("type") == "forfeit"]
        assert len(verfall) == 1
        assert verfall[0]["delta_hours"] == -6.0


class TestAbgrenzungZuAboveCap:
    async def test_above_cap_zahlt_weiterhin_aus(self, db, tenant, employee):
        """Gegenprobe: der bestehende Modus bleibt unverändert."""
        employee.overtime_payout_mode = "above_cap"
        await _anfangsbestand(db, tenant, employee, "26")

        await overtime_service.settle_month(db, tenant.id, employee, 2026, 6, None)

        entries = await _entries(db, employee)
        assert [e for e in entries if e.entry_type == "payout"]
        assert not [e for e in entries if e.entry_type == "forfeit"]
