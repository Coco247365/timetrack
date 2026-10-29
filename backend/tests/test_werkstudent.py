"""Beschaeftigungsart 'werkstudent': erfassen ja, messen nein.

Werkstudenten und Aushilfen stempeln wie alle anderen, haben aber kein
Tagessoll — also kein Minus, kein "unentschuldigt", kein Ueberstundenkonto —
und laufen nie in den DATEV-Export. Ihre Stunden gehen ueber den
Monats-Stundenzettel von Hand in die Abrechnung.
"""

from __future__ import annotations

from datetime import date

from zeiterfassung.policy import overtime as pol
from zeiterfassung.policy.overtime import daily_target_minutes, hat_tagessoll


class TestKeinTagessoll:
    def test_werkstudent_hat_kein_soll(self) -> None:
        assert hat_tagessoll("werkstudent") is False
        assert daily_target_minutes(20.0, employment_type="werkstudent") == 0

    def test_wochenplan_wird_ignoriert(self) -> None:
        """Auch ein hinterlegter Wochenplan darf kein Soll erzeugen."""
        assert (
            daily_target_minutes(
                40.0,
                weekday=0,
                weekday_minutes=[480, 480, 480, 480, 480, 0, 0],
                employment_type="werkstudent",
            )
            == 0
        )

    def test_gross_klein_egal(self) -> None:
        assert daily_target_minutes(20.0, employment_type="WERKSTUDENT") == 0

    def test_andere_arten_unveraendert(self) -> None:
        """Regressionsschutz: Stunden- und Gehaltsempfaenger rechnen wie bisher."""
        assert hat_tagessoll("hourly") is True
        assert hat_tagessoll("salaried") is True
        assert hat_tagessoll(None) is True
        assert daily_target_minutes(40.0, employment_type="hourly") == 480
        assert daily_target_minutes(40.0) == 480
        assert (
            daily_target_minutes(38.5, weekday=4, weekday_minutes=[495, 495, 495, 495, 330, 0, 0])
            == 330
        )


class TestMonatsabschlussOhneSoll:
    """settle_month darf fuer Erfassung-ohne-Soll nichts buchen: sonst wuerde
    delta = Ist − 0 die komplette Ist-Zeit als Ueberstunden ansammeln und (Modus
    above_cap) als UEBERSTUNDEN_AUSZAHLUNG bis in den DATEV-Export auszahlen."""

    async def test_settle_month_bucht_nichts_fuer_werkstudent(self, db, tenant, employee) -> None:
        from datetime import UTC, date, datetime

        from sqlalchemy import select

        from zeiterfassung.models.clock import DailyTimesheet, TimesheetStatus
        from zeiterfassung.models.overtime import OvertimeEntry
        from zeiterfassung.models.payroll_booking import PayrollBooking
        from zeiterfassung.services.overtime_service import settle_month

        employee.employment_type = "werkstudent"
        employee.overtime_payout_mode = "above_cap"
        employee.overtime_cap_hours = 20.0
        now = datetime.now(tz=UTC)
        # 60 h Ist im Juni 2026 — ohne den Schutz wuerden 40 h ausgezahlt.
        for day in (1, 2, 3, 4, 5, 8, 9, 10, 11, 12, 15, 16):
            db.add(
                DailyTimesheet(
                    tenant_id=tenant.id,
                    employee_id=employee.id,
                    work_date=date(2026, 6, day),
                    net_work_minutes=300,
                    gross_work_minutes=300,
                    break_minutes=0,
                    overtime_minutes=0,
                    status=TimesheetStatus.COMPLETE,
                    computed_from_events=True,
                    created_at=now,
                    updated_at=now,
                )
            )
        await db.flush()

        await settle_month(db, tenant.id, employee, 2026, 6)

        entries = (
            (
                await db.execute(
                    select(OvertimeEntry).where(OvertimeEntry.employee_id == employee.id)
                )
            )
            .scalars()
            .all()
        )
        bookings = (
            (
                await db.execute(
                    select(PayrollBooking).where(PayrollBooking.employee_id == employee.id)
                )
            )
            .scalars()
            .all()
        )
        assert entries == []
        assert bookings == []

    async def test_settle_month_bucht_weiter_fuer_stundenempfaenger(
        self, db, tenant, employee
    ) -> None:
        """Regressionsschutz: normale Mitarbeiter werden weiter abgeschlossen."""
        from sqlalchemy import select

        from zeiterfassung.models.overtime import OvertimeEntry
        from zeiterfassung.services.overtime_service import settle_month

        employee.employment_type = "hourly"
        await settle_month(db, tenant.id, employee, 2026, 6)
        entries = (
            (
                await db.execute(
                    select(OvertimeEntry).where(OvertimeEntry.employee_id == employee.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(entries) == 1
        assert entries[0].entry_type == "accrual"


def test_werkstudent_gearbeiteter_tag_zaehlt_nicht_ins_konto():
    """Ohne Tagessoll darf die Arbeitszeit NICHT als Überstunde im Konto landen."""
    f = pol.DayFacts(
        day=date(2026, 8, 3),
        weekend=False,
        holiday=False,
        coverage=0.0,
        net_minutes=571,
        has_timesheet=True,
        worked=True,
        is_auto=False,
        is_past=True,
        is_today_or_past=True,
        within_cutoff=True,
    )
    res = pol.classify_day(f, 0, hat_soll=False)
    assert res is not None
    assert res.net_minutes == 571  # Ist bleibt im Verlauf sichtbar
    assert res.target_minutes == 0
    assert res.delta_minutes == 0  # aber kein Beitrag zum Saldo
    assert res.absent is False


def test_werkstudent_fehltag_ist_kein_minus_und_nicht_unentschuldigt():
    f = pol.DayFacts(
        day=date(2026, 8, 4),
        weekend=False,
        holiday=False,
        coverage=0.0,
        net_minutes=0,
        has_timesheet=True,
        worked=False,
        is_auto=False,
        is_past=True,
        is_today_or_past=True,
        within_cutoff=True,
    )
    assert pol.classify_day(f, 0, hat_soll=False) is None


def test_ohne_flag_bleibt_verhalten_unveraendert():
    """Regression: der Bug war genau, dass soll=0 die Netto-Zeit zur Überstunde macht."""
    f = pol.DayFacts(
        day=date(2026, 8, 3),
        weekend=False,
        holiday=False,
        coverage=0.0,
        net_minutes=571,
        has_timesheet=True,
        worked=True,
        is_auto=False,
        is_past=True,
        is_today_or_past=True,
        within_cutoff=True,
    )
    assert pol.classify_day(f, 0).delta_minutes == 571
