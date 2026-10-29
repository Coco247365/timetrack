"""Automatischer Neuabschluss bei rückwirkenden Änderungen (mit Export-Sperre)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select

from zeiterfassung.models.audit import AuditLog
from zeiterfassung.models.export import ExportFormat, ExportStatus, PayrollExport
from zeiterfassung.models.overtime import OvertimeEntry
from zeiterfassung.services import overtime_service


async def _resettle_audit_months(db, employee):
    rows = (
        (
            await db.execute(
                select(AuditLog).where(
                    AuditLog.resource_id == str(employee.id),
                    AuditLog.action == "overtime.resettle",
                )
            )
        )
        .scalars()
        .all()
    )
    return {(r.details["year"], r.details["month"]) for r in rows}


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


class TestAutoResettleIfNeeded:
    async def test_resettelt_abgeschlossenen_vormonat(self, db, tenant, employee):
        """Adjustment in einem bereits abgeschlossenen Vormonat -> Neuabschluss."""
        employee.overtime_payout_mode = "all"
        employee.counting_start_date = date(2026, 7, 1)
        await db.flush()
        await overtime_service.settle_month(db, tenant.id, employee, 2026, 6, None)
        alte = await _entries(db, employee, date(2026, 6, 1))
        assert len(alte) == 1  # nur accrual, kein positiver Saldo

        await overtime_service.add_adjustment(
            db, tenant.id, employee.id, Decimal("10"), "Nachtrag", date(2026, 6, 15), None
        )
        result = await overtime_service.auto_resettle_if_needed(
            db, tenant.id, employee.id, date(2026, 6, 15), None
        )
        assert result == "resettled"

        entries = await _entries(db, employee, date(2026, 6, 1))
        accruals = [e for e in entries if e.entry_type == "accrual"]
        payouts = [e for e in entries if e.entry_type == "payout"]
        assert len(accruals) == 1
        assert len(payouts) == 1
        assert sum(float(e.hours) for e in payouts) == -10.0

    async def test_export_sperre_verhindert_neuabschluss(self, db, tenant, employee):
        """Existiert für den Monat bereits ein Export -> keine Änderung, "exported"."""
        employee.overtime_payout_mode = "all"
        employee.counting_start_date = date(2026, 7, 1)
        await db.flush()
        await overtime_service.settle_month(db, tenant.id, employee, 2026, 6, None)
        vorher = await _entries(db, employee, date(2026, 6, 1))

        db.add(
            PayrollExport(
                tenant_id=tenant.id,
                year_month=date(2026, 6, 1),
                format=ExportFormat.DATEV_CSV,
                status=ExportStatus.GENERATED,
                generated_at=datetime.now(tz=UTC),
            )
        )
        await db.flush()

        await overtime_service.add_adjustment(
            db, tenant.id, employee.id, Decimal("10"), "Nachtrag", date(2026, 6, 15), None
        )
        result = await overtime_service.auto_resettle_if_needed(
            db, tenant.id, employee.id, date(2026, 6, 15), None
        )
        assert result == "exported"

        nachher = await _entries(db, employee, date(2026, 6, 1))
        # Keine neuen accrual/payout-Zeilen — nur das Adjustment kam zusätzlich dazu.
        assert len([e for e in nachher if e.entry_type in ("accrual", "payout")]) == len(
            [e for e in vorher if e.entry_type in ("accrual", "payout")]
        )

    async def test_kaskade_ueber_mehrere_abgeschlossene_monate(self, db, tenant, employee):
        """Zwei abgeschlossene Monate, Adjustment im ersten -> beide neu abgeschlossen."""
        employee.overtime_payout_mode = "all"
        # Kontobeginn erst ab August: Mai/Juni tragen keine eigene Tages-Ist/Soll-Differenz
        # bei (außerhalb des Stichtags) — der Saldo kommt hier isoliert aus dem Ledger.
        employee.counting_start_date = date(2026, 8, 1)
        await db.flush()
        await overtime_service.settle_month(db, tenant.id, employee, 2026, 5, None)
        await overtime_service.settle_month(db, tenant.id, employee, 2026, 6, None)

        await overtime_service.add_adjustment(
            db, tenant.id, employee.id, Decimal("10"), "Nachtrag", date(2026, 5, 15), None
        )
        result = await overtime_service.auto_resettle_if_needed(
            db, tenant.id, employee.id, date(2026, 5, 15), None
        )
        assert result == "resettled"

        # Beide Monate wurden per resettle_month neu abgeschlossen (Audit-Trail).
        assert (2026, 5) in await _resettle_audit_months(db, employee)
        assert (2026, 6) in await _resettle_audit_months(db, employee)

        mai_payouts = [
            e for e in await _entries(db, employee, date(2026, 5, 1)) if e.entry_type == "payout"
        ]
        assert len(mai_payouts) == 1
        assert sum(float(e.hours) for e in mai_payouts) == -10.0

    async def test_offener_aktueller_monat_tut_nichts(self, db, tenant, employee):
        """Datum im laufenden Monat -> None, keine Buchungen."""
        today = datetime.now(tz=UTC).date()
        result = await overtime_service.auto_resettle_if_needed(
            db, tenant.id, employee.id, today, None
        )
        assert result is None
        assert await _entries(db, employee, date(today.year, today.month, 1)) == []
