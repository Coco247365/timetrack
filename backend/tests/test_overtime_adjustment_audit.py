"""Audit-Trail für manuelle Überstunden-Korrekturen (BetrVG §87).

Der overtime_ledger ist als append-only/unveränderlich dokumentiert. Solange der
Admin-Endpunkt eine In-Place-Bearbeitung/Löschung anbietet, MUSS jede solche
Mutation im AuditLog nachvollziehbar sein (Vorher/Nachher bzw. Snapshot).
Regression-Schutz für zeiterfassung-qpc.
"""

from datetime import date
from decimal import Decimal

from sqlalchemy import select

from zeiterfassung.models.audit import AuditLog
from zeiterfassung.services import overtime_service as ot


async def _audit_rows(db, tenant_id, action):
    return list(
        (
            await db.execute(
                select(AuditLog).where(
                    AuditLog.tenant_id == tenant_id,
                    AuditLog.action == action,
                )
            )
        ).scalars()
    )


async def test_update_adjustment_writes_audit_before_after(db, tenant, employee):
    """update_adjustment protokolliert Vorher/Nachher und ändert den Eintrag."""
    actor = employee.id
    entry = await ot.add_adjustment(
        db, tenant.id, employee.id, Decimal("5"), "Anfangsbestand", date(2026, 1, 10)
    )

    updated = await ot.update_adjustment(
        db, tenant.id, entry.id, Decimal("3.5"), "Korrigiert", date(2026, 2, 15), actor_id=actor
    )

    assert updated is not None
    assert updated.hours == Decimal("3.50")
    assert updated.reason == "Korrigiert"
    assert updated.entry_date == date(2026, 2, 15)
    assert updated.year_month == date(2026, 2, 1)

    rows = await _audit_rows(db, tenant.id, "overtime.adjustment.update")
    assert len(rows) == 1
    row = rows[0]
    assert row.actor_id == actor
    assert row.resource_type == "overtime_ledger"
    assert row.resource_id == str(entry.id)
    assert row.details["before"] == {
        "hours": "5.0000",
        "reason": "Anfangsbestand",
        "entry_date": "2026-01-10",
        "year_month": "2026-01-01",
    }
    assert row.details["after"] == {
        "hours": "3.5000",
        "reason": "Korrigiert",
        "entry_date": "2026-02-15",
        "year_month": "2026-02-01",
    }


async def test_delete_adjustment_writes_audit_snapshot(db, tenant, employee):
    """delete_adjustment protokolliert einen vollständigen Snapshot und entfernt den Eintrag."""
    actor = employee.id
    entry = await ot.add_adjustment(
        db, tenant.id, employee.id, Decimal("-2.25"), "Zu viel gebucht", date(2026, 3, 3)
    )
    entry_id = entry.id

    ok = await ot.delete_adjustment(db, tenant.id, entry_id, actor_id=actor)

    assert ok is True
    # Eintrag ist weg …
    assert await ot._get_adjustment(db, tenant.id, entry_id) is None
    # … aber im Audit-Log nachvollziehbar.
    rows = await _audit_rows(db, tenant.id, "overtime.adjustment.delete")
    assert len(rows) == 1
    row = rows[0]
    assert row.actor_id == actor
    assert row.resource_id == str(entry_id)
    assert row.details["deleted"] == {
        "hours": "-2.2500",
        "reason": "Zu viel gebucht",
        "entry_date": "2026-03-03",
        "year_month": "2026-03-01",
    }


async def test_update_missing_entry_writes_no_audit(db, tenant, employee):
    """Nicht gefundene/keine Korrektur -> None, kein Audit-Eintrag."""
    import uuid

    result = await ot.update_adjustment(
        db, tenant.id, uuid.uuid4(), Decimal("1"), "x", date(2026, 1, 1), actor_id=employee.id
    )
    assert result is None
    assert await _audit_rows(db, tenant.id, "overtime.adjustment.update") == []
