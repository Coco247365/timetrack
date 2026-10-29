"""Versandprotokoll fuer den monatlichen Stundenzettel per E-Mail.

Ein Eintrag pro Zustellversuch. Idempotenz macht der Service: ein Monat
gilt als erledigt, sobald ein ``sent``-Eintrag existiert; ``error``- und
``skipped_no_email``-Eintraege duerfen sich wiederholen (naechster Lauf
versucht es erneut).
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import Date, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from zeiterfassung.models.base import Base, TimestampMixin, UUIDCompat, UUIDPrimaryKeyMixin


class TimesheetMailLog(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Ein Zustellversuch eines Monats-Stundenzettels."""

    __tablename__ = "timesheet_mail_log"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUIDCompat(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        UUIDCompat(), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Erster Tag des Berichtsmonats (z. B. 2026-07-01 fuer den Juli-Zettel).
    year_month: Mapped[date] = mapped_column(Date, nullable=False)
    sent_to: Mapped[str | None] = mapped_column(String(320), nullable=True)
    # "sent" | "skipped_no_email" | "error"
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    detail: Mapped[str | None] = mapped_column(String(500), nullable=True)

    def __repr__(self) -> str:
        return f"<TimesheetMailLog {self.employee_id} {self.year_month} {self.status}>"
