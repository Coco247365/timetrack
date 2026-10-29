"""Gemeinsames Vokabular der Policy-Schicht: ``Violation`` + ``Severity``.

Jede Regel gibt entweder ``None`` (alles in Ordnung) oder eine ``Violation``
zurück. Der Aggregator (``policy.check``) sammelt ALLE Violations einer Operation
ein — so sieht der Nutzer sofort *jeden* Verstoß, statt sich von Fehler zu Fehler
durchzuhangeln.

Härte steckt in ``severity``:
  * ``BLOCK`` — die Operation wird verhindert (die API übersetzt das nach 409/422).
  * ``WARN``  — nur Hinweis/Markierung, die Operation läuft trotzdem durch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Severity(StrEnum):
    BLOCK = "BLOCK"
    WARN = "WARN"


@dataclass(frozen=True)
class Violation:
    """Ein einzelner Regelverstoß.

    Attributes:
        code:     stabiler Maschinen-Code (z.B. ``"absence.overlap"``) — fürs
                  Frontend/Logging, ändert sich nicht mit dem Text.
        severity: BLOCK oder WARN.
        message:  menschenlesbarer Text (wird im Frontend angezeigt).
        refs:     strukturierter Kontext fürs Frontend, z.B. der kollidierende
                  Eintrag (ID/Zeitraum), damit die UI gezielt darauf verweisen kann
                  ("Zeitraum belegt — erst diesen Eintrag löschen").
    """

    code: str
    severity: Severity
    message: str
    refs: dict = field(default_factory=dict)

    @classmethod
    def block(cls, code: str, message: str, **refs: object) -> Violation:
        """Erzeuge eine blockierende Violation (kurzform)."""
        return cls(code=code, severity=Severity.BLOCK, message=message, refs=dict(refs))

    @classmethod
    def warn(cls, code: str, message: str, **refs: object) -> Violation:
        """Erzeuge eine warnende Violation (kurzform)."""
        return cls(code=code, severity=Severity.WARN, message=message, refs=dict(refs))

    @property
    def is_block(self) -> bool:
        return self.severity is Severity.BLOCK
