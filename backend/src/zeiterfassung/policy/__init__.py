"""Policy-Schicht — zentrale Geschäfts-/Konfliktregeln.

Ein Ort für "was ist erlaubt / was kollidiert": reine, testbare Regeln ohne DB-
oder HTTP-Zugriff. Services laden die Daten, rufen die Regeln, persistieren; die
API übersetzt blockierende Verstöße in HTTP-Fehler.

Aufbau:
  * ``violations`` — ``Violation`` + ``Severity`` (BLOCK/WARN), das Vokabular.
  * ``context``    — Wertobjekte (``AbsenceEntry``, ``BreakInterval``, ``DayPart``).
  * ``absence``    — Domäne A (Urlaub/Krank: Überschneidung, Resturlaub).
  * ``timekeeping``— Domäne B (Pausen: innerhalb Arbeitszeit, keine Überlappung).
  * ``statutory``  — Domäne D (ArbZG, Hülle um ``core.arbzg``).
  * ``permissions``— Domäne E (Sichtbarkeit/Scope/Self-Approval).
  * ``check``      — Aggregator, sammelt alle Verstöße einer Operation.

(Domäne C Überstunden ist reine Berechnung ohne Konfliktregeln und bleibt im
``overtime_service``.)
"""

from __future__ import annotations

from zeiterfassung.policy.context import (
    LIVING_STATUSES,
    AbsenceEntry,
    BreakInterval,
    DayPart,
)
from zeiterfassung.policy.violations import Severity, Violation

__all__ = [
    "LIVING_STATUSES",
    "AbsenceEntry",
    "BreakInterval",
    "DayPart",
    "Severity",
    "Violation",
]
