"""Mitarbeiter-Archiv: Ausgetretene werden automatisch archiviert.

Die Personalakte bleibt gespeichert (Aufbewahrungspflicht), aber der
Mitarbeiter faellt aus dem aktiven System: is_active=False, Status
'archived', NFC-Karte geloest, Login-Accounts deaktiviert. Austrittsdatum
entfernen = Wiedereintritt.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select

from zeiterfassung.core.security import hash_password
from zeiterfassung.models.account import Account
from zeiterfassung.models.audit import AuditLog
from zeiterfassung.schemas.user import UserUpdate
from zeiterfassung.services.user_service import (
    archive_due_terminations,
    update_user,
)

YESTERDAY = date.today() - timedelta(days=1)
TOMORROW = date.today() + timedelta(days=1)


async def _account_for(db, tenant, employee) -> Account:
    acc = Account(
        tenant_id=tenant.id,
        email=employee.email,
        hashed_password=hash_password("Passw0rd!"),
        is_superadmin=False,
        employee_id=employee.id,
        is_active=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    db.add(acc)
    await db.flush()
    return acc


class TestArchivierung:
    async def test_austritt_in_vergangenheit_archiviert_sofort(self, db, tenant, employee):
        acc = await _account_for(db, tenant, employee)
        assert employee.nfc_badge_id  # Fixture hat BADGE001

        await update_user(
            db,
            tenant.id,
            employee.id,
            UserUpdate(termination_date=YESTERDAY, termination_reason="RENTE"),
        )

        assert employee.employment_status == "archived"
        assert employee.is_active is False
        assert employee.nfc_badge_id is None  # Karte freigegeben
        assert employee.termination_reason == "RENTE"
        await db.refresh(acc)
        assert acc.is_active is False  # Login deaktiviert
        # Audit-Eintrag vorhanden?
        logs = (
            (await db.execute(select(AuditLog).where(AuditLog.action == "user.archive")))
            .scalars()
            .all()
        )
        assert len(logs) == 1
        assert logs[0].details["nfc_badge_freigegeben"] == "BADGE001"

    async def test_austritt_in_zukunft_bleibt_aktiv(self, db, tenant, employee):
        await update_user(
            db,
            tenant.id,
            employee.id,
            UserUpdate(termination_date=TOMORROW, termination_reason="BEFRISTUNG_ENDE"),
        )
        assert employee.is_active is True
        assert employee.employment_status == "active"

    async def test_taeglicher_lauf_archiviert_faellige(self, db, tenant, employee):
        # Austritt gestern, aber niemand hat die Akte angefasst -> Lauf greift.
        employee.termination_date = YESTERDAY
        await db.flush()
        n = await archive_due_terminations(db)
        assert n == 1
        assert employee.employment_status == "archived"
        assert employee.is_active is False
        # Idempotent: zweiter Lauf archiviert nichts erneut.
        assert await archive_due_terminations(db) == 0

    async def test_wiedereintritt_durch_datum_entfernen(self, db, tenant, employee):
        await update_user(
            db,
            tenant.id,
            employee.id,
            UserUpdate(termination_date=YESTERDAY, termination_reason="KUENDIGUNG"),
        )
        assert employee.employment_status == "archived"

        await update_user(db, tenant.id, employee.id, UserUpdate(termination_date=None))
        assert employee.employment_status == "active"
        assert employee.is_active is True
        assert employee.termination_reason is None
        # Karte bleibt geloest (muss bewusst neu zugewiesen werden).
        assert employee.nfc_badge_id is None
