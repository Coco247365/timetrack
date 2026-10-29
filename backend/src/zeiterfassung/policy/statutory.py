"""Domäne D — Gesetzlich / ArbZG.

Dünne Policy-Hülle um die Rechen-Engine in ``core/arbzg.py`` (dort bleibt die
konkrete Schwellen-/Grace-Logik — nicht duplizieren). Alle ArbZG-Befunde sind
**Warnungen**, niemals blockierend: geleistete Zeit lässt sich nicht "verbieten",
sie wird dokumentiert und der Tag wird FLAGGED. Die gesetzliche Einstufung
(info/warning/error) der Engine wandert in ``refs["arbzg_severity"]``.
"""

from __future__ import annotations

from datetime import date, datetime

from zeiterfassung.core import arbzg
from zeiterfassung.policy.violations import Violation


def effective_break_minutes(gross_work_minutes: int, punched_break_minutes: int) -> int:
    """Kleinste gesetzeskonforme Tages-Pause (ArbZG §4, netto-basiert).

    §4 bemisst sich an der ARBEITSZEIT (Anwesenheit minus Pausen), nicht an der
    Anwesenheitsspanne: >6 h Arbeitszeit → 30 min, >9 h → 45 min. Gestempelte
    Pausen zählen voll; angehoben wird nur so weit, bis die resultierende
    Arbeitszeit die Schwellen einhält — nie mehr als nötig:

    - 9:12 Anwesenheit mit 30 min gestempelt → Arbeitszeit 8:42 ≤ 9 h → 30 bleibt
    - 6:10 Anwesenheit mit 15 min gestempelt → Arbeitszeit 5:55 ≤ 6 h → 15 bleibt
    - 6:10 Anwesenheit ohne Pause → Arbeitszeit 6:10 > 6 h → 30 min angesetzt
    - 10:00 Anwesenheit mit 30 min → Arbeitszeit 9:30 > 9 h → auf 45 angehoben

    Gleiche Konformitätsregel wie ``arbzg.check_break_compliance`` — nach der
    Anhebung kann ein Tagesbogen nie mehr als §4-Verstoß geflaggt werden.
    """
    for candidate in (
        punched_break_minutes,
        max(punched_break_minutes, arbzg.MIN_BREAK_AFTER_6H_MINUTES),
        max(punched_break_minutes, arbzg.MIN_BREAK_AFTER_9H_MINUTES),
    ):
        net = gross_work_minutes - candidate
        if (
            net <= arbzg.BREAK_THRESHOLD_6H_MINUTES
            or (
                net <= arbzg.BREAK_THRESHOLD_9H_MINUTES
                and candidate >= arbzg.MIN_BREAK_AFTER_6H_MINUTES
            )
            or candidate >= arbzg.MIN_BREAK_AFTER_9H_MINUTES
        ):
            return candidate
    return max(punched_break_minutes, arbzg.MIN_BREAK_AFTER_9H_MINUTES)


# --- Betriebliche Pausenregel (strenger als das Gesetz, Beschluss 2026-08-24) ---
#
# Ab dem Stichtag gilt zusaetzlich zur ArbZG-Staffel: MEHR ALS 4 h Arbeitszeit
# -> mindestens 45 min Pause. "Mehr als" ist bewusst strikt: exakte
# 4-h-Halbtage (z. B. Auto-Profil 07:30-11:30) bleiben ohne Abzug.
#
# Der Stichtag ist zwingend: Korrekturen an Alt-Tagen laufen erneut durch
# recompute — ohne Datumsgrenze wuerden bereits abgerechnete Tage rueckwirkend
# andere Ist-Zeiten bekommen. Vor dem Stichtag gilt deshalb unveraendert nur
# die gesetzliche Staffel. Stichtag 28.08.2026: Entscheidung "sofort ab
# morgen" vom 27.08. — der August hat damit bewusst zwei Regime.
BETRIEB_BREAK_THRESHOLD_MINUTES = 4 * 60
BETRIEB_MIN_BREAK_MINUTES = 45
BETRIEB_BREAK_GUELTIG_AB = date(2026, 8, 28)


def effective_break_minutes_betrieb(
    gross_work_minutes: int, punched_break_minutes: int, *, work_date: date
) -> int:
    """Kleinste zulaessige Tages-Pause nach Gesetz UND Betriebsregel.

    Gleiche Konformitaets-Mechanik wie ``effective_break_minutes``: gestempelte
    Pausen zaehlen voll, angehoben wird auf den festen Mindestwert (nicht "bis
    zur Schwelle") — wer bei > 4 h Arbeitszeit 20 min stempelt, bekommt 45 min
    angesetzt, wer so viel stempelt, dass die Arbeitszeit unter 4 h faellt,
    behaelt seine gestempelte Pause. Die Betriebsregel (45 min ab > 4 h) liegt
    ueberall auf oder ueber der ArbZG-Staffel (max. 45 min), erfuellt sie also
    automatisch mit.
    """
    if work_date < BETRIEB_BREAK_GUELTIG_AB:
        return effective_break_minutes(gross_work_minutes, punched_break_minutes)
    if gross_work_minutes - punched_break_minutes <= BETRIEB_BREAK_THRESHOLD_MINUTES:
        return punched_break_minutes
    return max(punched_break_minutes, BETRIEB_MIN_BREAK_MINUTES)


def check_arbzg(
    gross_work_minutes: int,
    break_minutes: int,
    *,
    start_time: datetime | None = None,
    prev_day_end_time: datetime | None = None,
    net_work_minutes: int | None = None,
) -> list[Violation]:
    """Führe alle anwendbaren ArbZG-Checks aus und gib sie als WARN-Violations zurück.

    Deckt ab: §3 Höchstarbeitszeit (L1/L2), §4 Pflichtpausen (L3/L4), §5 Ruhezeit
    (L5 — nur wenn ``prev_day_end_time`` übergeben wird; ist derzeit bewusst NICHT
    verdrahtet). Die 15-min-Toleranz (L6) steckt in der Engine.

    ``net_work_minutes``: echte Netto-Arbeitszeit für §3 — bei geteilten
    Schichten zwingend übergeben, sonst zählt der Fallback ``gross − breaks``
    die Lücke zwischen den Sessions als Arbeit.
    """
    result = arbzg.validate_timesheet(
        gross_work_minutes,
        break_minutes,
        start_time=start_time,
        prev_day_end_time=prev_day_end_time,
        net_work_minutes=net_work_minutes,
    )
    return [
        Violation.warn(
            code=f"arbzg.{v.rule.replace(' ', '_').replace('§', 'par')}",
            message=v.message,
            arbzg_rule=v.rule,
            arbzg_severity=v.severity,  # "info" | "warning" | "error"
        )
        for v in result.violations
    ]
