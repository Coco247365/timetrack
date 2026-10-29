"""WorkTimeProfile — Arbeitszeit-Profile je Mandant (Spec 2026-08-14).

Zwei streng getrennte Typen, serverseitig an ``users.time_capture`` gebunden:

- ``kind='nfc'`` (Stempler): EIN erlaubtes Stempel-Fenster ``clock_start`` –
  ``clock_end`` ("HH:MM", lokale Mandanten-Zeit). NULL auf einer Seite =
  keine Kappung an dieser Seite. ``clock_end < clock_start`` ist erlaubt
  (Nachtschicht, z. B. 22:00–06:15).
- ``kind='auto'``: ``weekday_times`` JSON ``{"0": {"start", "end",
  "break_start"?, "break_end"?}, ..., "6": null}`` (0=Mo). Fehlender/
  null-Tag = frei (Soll 0).

Die Kappungs-/Auto-Fuellungs-Logik folgt in spaeteren Bausteinen — dieses
Modul ist nur das Datenmodell.
"""

from __future__ import annotations

import enum
import uuid

from sqlalchemy import Boolean, Enum, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from zeiterfassung.models.base import (
    Base,
    JSONBCompat,
    TimestampMixin,
    UUIDCompat,
    UUIDPrimaryKeyMixin,
)


class WorkTimeProfileKind(enum.StrEnum):
    NFC = "nfc"
    AUTO = "auto"


class WorkTimeProfile(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Arbeitszeit-Profil eines Mandanten (n Mitarbeiter je Profil)."""

    __tablename__ = "work_time_profiles"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_work_time_profiles_tenant_name"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUIDCompat(),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[WorkTimeProfileKind] = mapped_column(
        Enum(
            WorkTimeProfileKind,
            name="work_time_profile_kind_enum",
            native_enum=False,
            length=4,
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Nur kind='nfc': erlaubtes Stempel-Fenster (lokale Mandanten-Zeit).
    clock_start: Mapped[str | None] = mapped_column(
        String(5), nullable=True, comment="HH:MM; NULL = keine Kappung am Beginn"
    )
    clock_end: Mapped[str | None] = mapped_column(
        String(5), nullable=True, comment="HH:MM; NULL = keine Kappung am Ende"
    )

    # Nur kind='auto': Zeiten je Wochentag (0=Mo .. 6=So), null-Tag = frei.
    weekday_times: Mapped[dict | None] = mapped_column(JSONBCompat, nullable=True)

    # Nur kind='nfc' (Nachtrag 14.08.): Vertragsstunden + Tages-Soll je Wochentag
    # ({"0".."6": Minuten}, fehlender Tag = 0). Beide NULL = Bestandsprofil ohne
    # Stundenbindung; die CRUD-Validierung erzwingt Summe == Stunden × 60.
    # Auto-Profile lassen beide NULL — ihre Stunden werden aus weekday_times
    # berechnet (policy.worktime.auto_contracted_hours_per_week).
    contracted_hours_per_week: Mapped[float | None] = mapped_column(
        Numeric(5, 2),
        nullable=True,
        comment="Nur kind='nfc'; NULL = Bestandsprofil ohne Stundenbindung",
    )
    weekday_target_minutes: Mapped[dict | None] = mapped_column(
        JSONBCompat,
        nullable=True,
        comment='Nur kind=\'nfc\': Tages-Soll je Wochentag {"0".."6": Minuten}',
    )

    def __repr__(self) -> str:
        return f"<WorkTimeProfile id={self.id} name={self.name!r} kind={self.kind}>"
