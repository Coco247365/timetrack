"""Manuelle Lohnbuchungen (Pauschalen / Beträge / Sonderzahlungen).

Reine Export-/Lohn-Positionen je Mitarbeiter und Monat: Lohnart + Wert.
Fließen in den DATEV-LODAS-Export, NICHT in die Zeit-/Überstundenkonten
(echte Arbeitszeit wird über den Tages-Editor gepflegt).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from zeiterfassung.models.base import Base, UUIDCompat, UUIDPrimaryKeyMixin


class PayrollBooking(UUIDPrimaryKeyMixin, Base):
    """Eine manuelle Lohnart-Buchung für einen Mitarbeiter in einem Monat."""

    __tablename__ = "payroll_bookings"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUIDCompat(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        UUIDCompat(), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Erster Tag des Abrechnungsmonats, z. B. 2026-06-01
    year_month: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    # Verweist auf wage_type_mappings.internal_code (DATEV-Nr + Einheit kommen daher).
    wage_type_code: Mapped[str] = mapped_column(String(64), nullable=False)
    # Stunden ODER Betrag ODER Tage (je nach Einheit der Lohnart); darf negativ sein (Abzug).
    value: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    # Optionales Datum nur zur Dokumentation (z. B. welches Wochenende) — NICHT im Export.
    booking_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUIDCompat(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    def __repr__(self) -> str:
        return (
            f"<PayrollBooking emp={self.employee_id} {self.year_month} "
            f"{self.wage_type_code}={self.value}>"
        )
