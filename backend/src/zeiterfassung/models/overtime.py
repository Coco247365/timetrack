"""Überstunden-Log (append-only Ledger).

Datiertes, unveränderliches Log. Der Saldo ergibt sich aus dem Replay der
Einträge (nicht aus einem gespeicherten Wert), damit vergangene Monate stabil
bleiben und Exporte rückrechenbar sind.

Eintragstypen:
- accrual    : Monats-Ansammlung (netto Ist−Soll), beim Abschluss eingefroren
- payout     : Auszahlung (negativ), erzeugt zusätzlich eine Lohnbuchung UEBERSTUNDEN
- adjustment : manuelle Korrektur / Anfangsbestand (±)
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from zeiterfassung.models.base import Base, UUIDCompat, UUIDPrimaryKeyMixin


class OvertimeEntry(UUIDPrimaryKeyMixin, Base):
    """Ein Eintrag im Überstunden-Log."""

    __tablename__ = "overtime_ledger"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUIDCompat(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        UUIDCompat(), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Abrechnungsmonat, zu dem der Eintrag gehört (1. des Monats).
    year_month: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    # Tatsächliches Ereignisdatum (für die chronologische Reihenfolge im Replay).
    entry_date: Mapped[date] = mapped_column(Date, nullable=False)
    # accrual | payout | adjustment
    entry_type: Mapped[str] = mapped_column(String(12), nullable=False)
    # Stunden mit Vorzeichen: Ansammlung +, Auszahlung −, Korrektur ±.
    # 4 Nachkommastellen: 2 waeren ein 0,6-Minuten-Raster — eine echte Minute
    # (1/60 h) ist darin nicht darstellbar, die Anzeige kippte um ±1 min.
    hours: Mapped[Decimal] = mapped_column(Numeric(9, 4), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUIDCompat(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    def __repr__(self) -> str:
        return (
            f"<OvertimeEntry emp={self.employee_id} {self.year_month} "
            f"{self.entry_type} {self.hours}>"
        )
