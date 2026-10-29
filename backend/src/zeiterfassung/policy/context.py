"""Wertobjekte (plain data) für die Policy-Schicht.

Die Regeln in ``policy/`` sind REINE Funktionen: sie bekommen bereits geladene
Daten als schlichte Wertobjekte und geben ``Violation``s zurück — **kein DB-,
kein HTTP-Zugriff**. Die Services mappen ihre ORM-Objekte auf diese Wertobjekte,
bevor sie die Regeln aufrufen. Dadurch sind die Regeln ohne Datenbank testbar
und können domänenübergreifend kombiniert werden (z.B. Urlaub UND Krank in einer
Liste prüfen).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum


class DayPart(StrEnum):
    """Tagesanteil einer Abwesenheit.

    Urlaub wird halbtags-genau geführt: ein halber Tag ist möglich und
    eintragbar. Feiner als halbtags (freie Stundenzahl) ist bewusst NICHT
    vorgesehen — kann später ergänzt werden, ohne die Regeln umzubauen.
    """

    FULL = "FULL"  # ganzer Tag
    AM = "AM"  # nur Vormittag
    PM = "PM"  # nur Nachmittag

    @property
    def fraction(self) -> float:
        """Anteil eines Arbeitstages (für Stunden-/Saldo-Berechnung).

        FULL = 1,0 · AM/PM = 0,5. Die tatsächlichen Stunden ergeben sich im
        Service aus ``fraction * Soll-Stunden-pro-Tag`` (= Wochenstunden/5),
        damit es auch für Teilzeit korrekt rechnet.
        """
        return 1.0 if self is DayPart.FULL else 0.5


# Antragsstatus, die einen Zeitraum "belegen" und daher bei Überschneidungs-
# prüfungen zählen. Storniert/abgelehnt/Entwurf zählen NICHT.
LIVING_STATUSES: frozenset[str] = frozenset({"SUBMITTED", "APPROVED", "ESCALATED"})


@dataclass(frozen=True)
class AbsenceEntry:
    """Eine bestehende/geplante Abwesenheit — nur die Felder, die Regeln brauchen.

    ``request_type``/``status`` sind die String-Werte der jeweiligen Enums
    (z.B. ``"VACATION"``, ``"APPROVED"``), damit die Policy nicht an die
    ORM-Enums gekoppelt ist.
    """

    date_from: date
    date_to: date
    day_part: DayPart = DayPart.FULL
    request_type: str | None = None
    status: str | None = None
    request_id: str | None = None  # zum Rückverweis im Frontend


@dataclass(frozen=True)
class BreakInterval:
    """Eine Pause als Von/Bis (Zeitpunkte, i.d.R. am selben Arbeitstag)."""

    start: datetime
    end: datetime
