"""Domäne A — Abwesenheit / Urlaub / Krank.

Reine Regeln rund um Abwesenheits-Anträge. Einzelfeld-/Formprüfungen (Datum
sortiert, keine Vergangenheit) bleiben bewusst im Pydantic-Schema
(``schemas/absence.py``, R-A1/R-A2) — hier stehen die *fachlichen*,
domänenübergreifenden Regeln (Überschneidung, Resturlaub).
"""

from __future__ import annotations

from collections.abc import Iterable

from zeiterfassung.policy.context import LIVING_STATUSES, AbsenceEntry, DayPart
from zeiterfassung.policy.violations import Violation


def _kollidiert(a: AbsenceEntry, b: AbsenceEntry) -> bool:
    """── R-A3 (Kern): Überschneiden sich zwei Abwesenheiten halbtags-genau? ──

    Zwei Einträge kollidieren nur, wenn sich ihre Datumsbereiche überlappen UND
    sie denselben Tagesteil beanspruchen:
      * Ganzer Tag (FULL) kollidiert mit allem.
      * Sonst nur gleicher Halbtag (AM/AM bzw. PM/PM).
    Vormittag + Nachmittag am selben Datum ist erlaubt (z.B. Urlaub früh, Arzt
    spät) — ebenso ein AM-Antrag über mehrere Tage neben einem PM-Antrag über
    dieselben Tage.
    """
    if a.date_to < b.date_from or b.date_to < a.date_from:
        return False  # keine Datums-Überlappung
    if a.day_part is DayPart.FULL or b.day_part is DayPart.FULL:
        return True  # ganzer Tag blockt alles
    return a.day_part == b.day_part  # sonst nur gleicher Halbtag


def check_no_overlap(
    new: AbsenceEntry,
    existing: Iterable[AbsenceEntry],
) -> Violation | None:
    """── R-A3: Abwesenheiten dürfen sich nicht überschneiden (HART, halbtags) ──

    Gilt typ-übergreifend: Urlaub kollidiert auch mit Krank (SICK_LEAVE) usw.,
    solange der bestehende Antrag noch "lebt" (eingereicht/genehmigt/eskaliert).
    Blockierend — das Frontend zeigt anhand von ``refs`` den belegten Zeitraum an
    ("Zeitraum belegt, erst löschen"). Der Eintrag selbst wird nie als Konflikt
    mit sich gewertet (gleiche ``request_id`` wird übersprungen).
    """
    for e in existing:
        if e.status is not None and e.status not in LIVING_STATUSES:
            continue
        if new.request_id is not None and e.request_id == new.request_id:
            continue
        if _kollidiert(new, e):
            return Violation.block(
                "absence.overlap",
                f"Im Zeitraum {e.date_from:%d.%m.%Y}–{e.date_to:%d.%m.%Y} ist bereits eine "
                f"Abwesenheit eingetragen. Bitte diesen Eintrag zuerst entfernen.",
                conflict_id=e.request_id,
                conflict_from=e.date_from.isoformat(),
                conflict_to=e.date_to.isoformat(),
                conflict_type=e.request_type,
                conflict_day_part=e.day_part.value,
            )
    return None


def check_balance_warning(remaining_hours_after: float) -> Violation | None:
    """── R-A9: Resturlaub-Warnung (WARN, blockiert NICHT) ──

    Der Saldo wird in Stunden geführt (halber Tag = 0,5 × Soll/Tag). Würde der
    neue Antrag das Kontingent überziehen, gibt es nur eine **Warnung** — der
    Vorgesetzte kann bewusst übersteuern. ``remaining_hours_after`` ist der
    Reststand, der NACH Anrechnung des neuen Antrags übrig bliebe (vom Service
    berechnet, inkl. Feiertage/Bundesland).
    """
    if remaining_hours_after < 0:
        fehlend = abs(remaining_hours_after)
        return Violation.warn(
            "absence.balance_exceeded",
            f"Resturlaub reicht nicht: {fehlend:.1f} h über dem Kontingent. "
            f"Genehmigung nur mit bewusster Übersteuerung.",
            missing_hours=round(fehlend, 2),
        )
    return None
