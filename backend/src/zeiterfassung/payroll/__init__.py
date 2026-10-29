"""Payroll-Regel-Schicht — Zuordnung von Arbeitszeit zu Export-Kategorien.

Reine, testbare Regeln ohne DB- oder HTTP-Zugriff, nach dem Muster von
``policy/``: Services laden die Rohdaten (Punch-Events, Feiertage, Abwesen-
heiten), rufen die Funktionen hier auf und persistieren/exportieren das
Ergebnis.

WICHTIG: Diese Schicht kennt bewusst KEINE DATEV-Lohnart-Nummern — nur
interne Kategorie-Codes (z.B. ``NORMALE_ARBEITSZEIT``). Die Zuordnung
Code → Lohnart-Nummer trägt der Nutzer später in den Einstellungen ein.

Aufbau:
  * ``categories`` — Registry der Export-Kategorien (Code, Label, Einheit).
  * ``rules``      — reine Berechnungslogik (Segmente, Zeitfenster, Tageszuordnung).
"""

from __future__ import annotations

from zeiterfassung.payroll.categories import Category, all_categories, get_category
from zeiterfassung.payroll.rules import (
    SurchargeConfig,
    WorkSegment,
    absence_minutes,
    categorize_day,
    overlap_minutes,
    paid_holiday_minutes,
    segments_from_events,
)

__all__ = [
    "Category",
    "SurchargeConfig",
    "WorkSegment",
    "absence_minutes",
    "all_categories",
    "categorize_day",
    "get_category",
    "overlap_minutes",
    "paid_holiday_minutes",
    "segments_from_events",
]
