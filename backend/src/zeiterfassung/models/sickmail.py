"""SickMailReport — per E-Mail eingegangene Krankmeldungen.

Jede Mail an das Krankmeldungs-Postfach wird hier abgelegt (Rohdaten +
Parser-Ergebnis). Bei eindeutigem Absender-Match entsteht zusaetzlich ein
regulaerer SICK_LEAVE-Antrag (SUBMITTED); ohne Match bleibt der Eintrag
"NEW" und HR ordnet ihn manuell zu. Gesundheitsdaten: Zugriff nur mit
``medical:read`` (bzw. Admin).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from zeiterfassung.models.base import Base, TimestampMixin, UUIDCompat, UUIDPrimaryKeyMixin


class SickMailReport(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Eine eingegangene Krankmeldungs-Mail."""

    __tablename__ = "sick_mail_reports"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUIDCompat(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Eindeutige Nachrichten-ID des Mail-Systems — verhindert Doppel-Import.
    message_id: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    sender_email: Mapped[str] = mapped_column(String(255), nullable=False)
    subject: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Nur der Klartext (gekuerzt) — genug fuer die manuelle Pruefung.
    body_preview: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Parser-Ergebnis (Vorschlag): Zeitraum + welches Muster gezogen hat.
    parsed_date_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    parsed_date_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    parsed_hint: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Zuordnung: erkannter/zugeordneter Mitarbeiter + ggf. erzeugter Antrag.
    employee_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDCompat(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    absence_request_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDCompat(), ForeignKey("approval_requests.id", ondelete="SET NULL"), nullable=True
    )
    # NEW (unzugeordnet) | LINKED (Antrag erzeugt) | DISMISSED (verworfen)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="NEW", index=True)
    # Wie kam die Zuordnung zustande: "auto" (Absender erkannt) | "manual" (HR).
    assignment: Mapped[str | None] = mapped_column(String(16), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SickMailReport {self.sender_email} {self.status}>"
