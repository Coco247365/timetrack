"""Monatsabschluss: idempotent + mode=all ohne Doppel-Buchung (Spec 2026-07-09)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select

from zeiterfassung.models.clock import DailyTimesheet
from zeiterfassung.models.overtime import OvertimeEntry
from zeiterfassung.models.payroll_booking import PayrollBooking
from zeiterfassung.services import overtime_service


async def _entries(db, emp, ym):
    rows = (
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
    return rows


class TestSettleMonth:
    async def test_idempotent(self, db, tenant, employee):
        employee.overtime_payout_mode = "above_cap"
        employee.overtime_cap_hours = 20.0
        await db.flush()
        ym = date(2026, 6, 1)
        await overtime_service.settle_month(db, tenant.id, employee, 2026, 6, None)
        n1 = len(await _entries(db, employee, ym))
        await overtime_service.settle_month(db, tenant.id, employee, 2026, 6, None)
        n2 = len(await _entries(db, employee, ym))
        assert n1 == n2  # zweiter Lauf erzeugt NICHTS

    async def test_mode_all_keine_payroll_booking(self, db, tenant, employee):
        employee.overtime_payout_mode = "all"
        # Kontobeginn erst ab Juli: Juni soll keinen eigenen (stark negativen)
        # Tages-Soll/Ist-Anteil beisteuern (kein Tagesbogen für Juni angelegt) —
        # so bleibt der Saldo isoliert vom Vormonats-Ledgereintrag bestimmt.
        employee.counting_start_date = date(2026, 7, 1)
        await db.flush()
        # Positiven Saldo herstellen: direkte Ledger-Zeile Vormonat
        now = datetime.now(tz=UTC)
        db.add(
            OvertimeEntry(
                tenant_id=tenant.id,
                employee_id=employee.id,
                year_month=date(2026, 5, 1),
                entry_date=date(2026, 5, 31),
                entry_type="accrual",
                hours=5,
                reason="Test",
                created_by=None,
                created_at=now,
            )
        )
        await db.flush()
        await overtime_service.settle_month(db, tenant.id, employee, 2026, 6, None)
        bookings = (
            (
                await db.execute(
                    select(PayrollBooking).where(PayrollBooking.employee_id == employee.id)
                )
            )
            .scalars()
            .all()
        )
        assert bookings == []  # mode=all: Staffel liefert die Export-Zeilen, keine Buchung
        # aber der Ledger-payout existiert (Konto ausgeglichen):
        assert any(e.entry_type == "payout" for e in await _entries(db, employee, date(2026, 6, 1)))


class TestPayrollCutoff:
    async def test_altsystem_monat_zahlt_ohne_lohnbuchung(
        self, monkeypatch, db, tenant, employee
    ):
        """Monate vor dem Lohnlauf-Stichtag: Auszahlung nur im Ledger.

        Altsystem hat diese Monate bereits vergütet — eine PayrollBooking würde
        die Stunden ein zweites Mal in den DATEV-Export tragen.
        """
        from types import SimpleNamespace

        monkeypatch.setattr(
            overtime_service,
            "get_settings",
            lambda: SimpleNamespace(payroll_cutoff_date=date(2026, 8, 1)),
        )
        employee.overtime_payout_mode = "above_cap"
        employee.overtime_cap_hours = 0.0
        employee.counting_start_date = date(2026, 7, 1)
        await db.flush()
        now = datetime.now(tz=UTC)
        db.add(
            OvertimeEntry(
                tenant_id=tenant.id,
                employee_id=employee.id,
                year_month=date(2026, 5, 1),
                entry_date=date(2026, 5, 31),
                entry_type="accrual",
                hours=5,
                reason="Test",
                created_by=None,
                created_at=now,
            )
        )
        await db.flush()
        await overtime_service.settle_month(db, tenant.id, employee, 2026, 6, None)
        payouts = [
            e for e in await _entries(db, employee, date(2026, 6, 1))
            if e.entry_type == "payout"
        ]
        assert len(payouts) == 1
        assert "Altsystem" in (payouts[0].reason or "")
        bookings = (
            (
                await db.execute(
                    select(PayrollBooking).where(PayrollBooking.employee_id == employee.id)
                )
            )
            .scalars()
            .all()
        )
        assert bookings == []  # Altsystem hat bezahlt — keine zweite Buchung


class TestSettlePreviousMonthAll:
    async def test_settle_previous_month_all(self, db, tenant, employee):
        # Auto-Abschluss läuft nur für Soll-Tenants (Finding 1).
        tenant.settings = {**(tenant.settings or {}), "zuschlaege": {"export_basis": "soll"}}
        await db.flush()
        # Heute-Datum im Test nicht mockbar noetig: Funktion nimmt referenz_datum-Param.
        n = await overtime_service.settle_previous_month_all(db, today=date(2026, 7, 15))
        assert n >= 1  # employee wurde fuer Juni abgeschlossen
        # idempotent: zweiter Lauf am selben Tag schliesst nichts mehr ab
        assert await overtime_service.settle_previous_month_all(db, today=date(2026, 7, 15)) == 0

    async def test_ist_tenant_wird_uebersprungen(self, db, tenant, employee):
        """Finding 1: Ist-Tenant (Default) + above_cap-User -> kein Auto-Abschluss.

        Sonst würde der Bypass die above_cap-Buchung (UEBERSTUNDEN_AUSZAHLUNG)
        zusätzlich in den Ist-Export lassen, der die Ist-Stunden schon voll
        bucht -> Doppelvergütung.
        """
        employee.overtime_payout_mode = "above_cap"
        employee.overtime_cap_hours = 0.0
        await db.flush()
        # tenant.settings bleibt None -> export_basis Default "ist".
        n = await overtime_service.settle_previous_month_all(db, today=date(2026, 7, 15))
        assert n == 0
        entries = await _entries(db, employee, date(2026, 6, 1))
        assert entries == []
        bookings = (
            (
                await db.execute(
                    select(PayrollBooking).where(PayrollBooking.employee_id == employee.id)
                )
            )
            .scalars()
            .all()
        )
        assert bookings == []

    async def test_alle_offenen_vormonate_werden_abgeschlossen(self, db, tenant, employee):
        """Finding 2: nicht nur der unmittelbare Vormonat, sondern ALLE offenen."""
        tenant.settings = {**(tenant.settings or {}), "zuschlaege": {"export_basis": "soll"}}
        employee.counting_start_date = date(2026, 5, 1)
        await db.flush()
        # Heute = 15.07 -> Mai UND Juni sind offene Vormonate.
        n = await overtime_service.settle_previous_month_all(db, today=date(2026, 7, 15))
        assert n >= 2
        assert await _entries(db, employee, date(2026, 5, 1))
        assert await _entries(db, employee, date(2026, 6, 1))
        # zweiter Lauf: nichts Neues mehr.
        assert await overtime_service.settle_previous_month_all(db, today=date(2026, 7, 15)) == 0


class TestResettleMonth:
    async def test_nachtrag_wirkt_erst_nach_resettle(self, db, tenant, employee):
        """Anfangsbestand nach dem Abschluss wirkt erst nach resettle_month."""
        employee.overtime_payout_mode = "all"
        # Kontobeginn erst ab Juli: Juni soll aus der eigenen Tagesrechnung 0
        # beisteuern (kein Tagesbogen fuer Juni angelegt), Saldo kommt hier
        # bewusst nur aus den Ledger-Zeilen (accrual/adjustment).
        employee.counting_start_date = date(2026, 7, 1)
        await db.flush()
        await overtime_service.settle_month(db, tenant.id, employee, 2026, 6, None)
        alte_entries = await _entries(db, employee, date(2026, 6, 1))
        assert len(alte_entries) == 1  # nur accrual, kein positiver Saldo -> kein payout

        # Nachtrag: Anfangsbestand im selben Monat nachgebucht.
        await overtime_service.add_adjustment(
            db, tenant.id, employee.id, Decimal("10"), "Anfangsbestand", date(2026, 6, 15), None
        )
        # settle_month allein tut nichts mehr (Monat ist gesperrt):
        await overtime_service.settle_month(db, tenant.id, employee, 2026, 6, None)
        assert len(await _entries(db, employee, date(2026, 6, 1))) == 2  # accrual + adjustment

        result = await overtime_service.resettle_month(db, tenant.id, employee, 2026, 6, None)
        assert result["alte_auszahlung"] == Decimal("0.00")
        assert result["neue_auszahlung"] == Decimal("10.00")

        entries = await _entries(db, employee, date(2026, 6, 1))
        accruals = [e for e in entries if e.entry_type == "accrual"]
        payouts = [e for e in entries if e.entry_type == "payout"]
        adjustments = [e for e in entries if e.entry_type == "adjustment"]
        assert len(accruals) == 1
        assert len(payouts) == 1
        assert len(adjustments) == 1  # Adjustment überlebt den Reset

    async def test_alte_payroll_booking_wird_ersetzt(self, db, tenant, employee):
        employee.overtime_payout_mode = "above_cap"
        employee.overtime_cap_hours = 0.0
        employee.counting_start_date = date(2026, 7, 1)
        await db.flush()
        await overtime_service.add_adjustment(
            db, tenant.id, employee.id, Decimal("5"), "Start", date(2026, 6, 1), None
        )
        await overtime_service.settle_month(db, tenant.id, employee, 2026, 6, None)
        bookings = (
            (
                await db.execute(
                    select(PayrollBooking).where(PayrollBooking.employee_id == employee.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(bookings) == 1
        alte_wert = bookings[0].value

        await overtime_service.add_adjustment(
            db, tenant.id, employee.id, Decimal("3"), "Nachtrag", date(2026, 6, 2), None
        )
        result = await overtime_service.resettle_month(db, tenant.id, employee, 2026, 6, None)
        assert result["alte_auszahlung"] == alte_wert
        assert result["neue_auszahlung"] == alte_wert + Decimal("3.00")

        bookings = (
            (
                await db.execute(
                    select(PayrollBooking).where(PayrollBooking.employee_id == employee.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(bookings) == 1  # alte Buchung ersetzt, nicht verdoppelt
        assert bookings[0].value == result["neue_auszahlung"]


def _ts(tenant_id, emp_id, d, net):
    """Hilfsfunktion zum Erstellen eines DailyTimesheet für Tests."""
    now = datetime.now(tz=UTC)
    return DailyTimesheet(
        tenant_id=tenant_id,
        employee_id=emp_id,
        work_date=d,
        start_time=datetime(d.year, d.month, d.day, 8, 0, tzinfo=UTC),
        break_minutes=0,
        net_work_minutes=net,
        gross_work_minutes=net,
        overtime_minutes=0,
        is_holiday_work=False,
        created_at=now,
        updated_at=now,
    )


class TestSettleBundesland:  # Issue #68
    """06.01. (Heilige Drei Koenige) ist Feiertag in BY, NICHT in HH.

    Auto-Erfassung + genau EIN Tagesbogen mit 4h am 06.01.2026 (Di):
    - BY: Feiertag -> Policy #21: Arbeit an Feiertag zaehlt nicht ins Konto -> Saldo 0.
    - HH: normaler Arbeitstag -> 4h Ist - 8h Soll = -4h.
    Vor dem Fix rechnete settle_month IMMER mit HH.
    """

    async def _settle_and_balance(self, db, tenant, employee) -> Decimal:
        employee.time_capture = "auto"  # keine Fehltage-Minuten fuer leere Tage
        db.add(_ts(tenant.id, employee.id, date(2026, 1, 6), 240))
        await db.flush()
        await overtime_service.settle_month(db, tenant.id, employee, 2026, 1, None)
        entries = await _entries(db, employee, date(2026, 1, 1))
        accruals = [e for e in entries if e.entry_type == "accrual"]
        assert len(accruals) == 1
        return Decimal(str(accruals[0].hours))

    async def test_by_feiertag_zaehlt_nicht(self, db, tenant, employee):
        tenant.bundesland = "BY"
        assert await self._settle_and_balance(db, tenant, employee) == Decimal("0.00")

    async def test_hh_gleicher_tag_ist_arbeitstag(self, db, tenant, employee):
        tenant.bundesland = "HH"
        assert await self._settle_and_balance(db, tenant, employee) == Decimal("-4.00")


class TestSettleBackfill:
    """Beim rueckwirkenden Abschliessen (Backfill) duerfen Folgemonats-
    Eintraege den Saldo NICHT beeinflussen — sonst landet der gesamte
    Ueberlauf kumuliert im ersten Monat (Fund vom 2026-07-14, PN42:
    Mai-Auszahlung 11,52 rutschte in den April)."""

    async def test_folgemonat_zaehlt_nicht_in_die_auszahlung(self, db, tenant, employee):
        from decimal import Decimal

        from zeiterfassung.models.overtime import OvertimeEntry

        employee.overtime_payout_mode = "above_cap"
        employee.overtime_cap_hours = 20.0
        employee.time_capture = "auto"
        now = datetime.now(tz=UTC)
        # Bestand April: +25h (Adjustment) -> Auszahlung April muss 5h sein.
        db.add(
            OvertimeEntry(
                tenant_id=tenant.id,
                employee_id=employee.id,
                year_month=date(2026, 4, 1),
                entry_date=date(2026, 4, 1),
                entry_type="adjustment",
                hours=Decimal("25.00"),
                reason="Test-Bestand",
                created_at=now,
            )
        )
        # Folgemonat Mai hat BEREITS eine Ansammlung (+7h) im Ledger.
        db.add(
            OvertimeEntry(
                tenant_id=tenant.id,
                employee_id=employee.id,
                year_month=date(2026, 5, 1),
                entry_date=date(2026, 5, 31),
                entry_type="accrual",
                hours=Decimal("7.00"),
                reason="Monatsansammlung (netto)",
                created_at=now,
            )
        )
        await db.flush()
        await overtime_service.settle_month(db, tenant.id, employee, 2026, 4, None)
        entries = await _entries(db, employee, date(2026, 4, 1))
        payouts = [e for e in entries if e.entry_type == "payout"]
        assert len(payouts) == 1
        # 25h - 20h Kappe = 5h — NICHT 12h (25+7-20).
        assert Decimal(str(payouts[0].hours)) == Decimal("-5.00")
