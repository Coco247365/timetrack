"""Kiosk-Kurzmeldungen: Hinweise auf dem Terminal (#189).

Zwei Arten, unterschieden ueber ``employee_id``:
- ``employee_id IS NULL`` -> Rundmeldung fuer ALLE (Ruhebildschirm).
- ``employee_id`` gesetzt -> persoenliche Meldung (nach dem Stempeln).
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import Date, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from zeiterfassung.models.base import Base, TimestampMixin, UUIDCompat, UUIDPrimaryKeyMixin


class KioskMessage(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Eine Kurzmeldung fuer das Terminal."""

    __tablename__ = "kiosk_messages"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUIDCompat(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # NULL = Rundmeldung fuer alle; gesetzt = persoenlich fuer diesen Mitarbeiter.
    employee_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDCompat(), ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    text: Mapped[str] = mapped_column(String(280), nullable=False)
    # Ab diesem Tag anzeigen; NULL = sofort. Erlaubt Planung im Voraus.
    active_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Bis einschliesslich diesem Tag anzeigen; NULL = unbefristet.
    active_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_by_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDCompat(), ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )

    def __repr__(self) -> str:
        scope = "all" if self.employee_id is None else str(self.employee_id)
        return f"<KioskMessage {scope} {self.text[:20]!r}>"
