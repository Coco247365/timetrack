"""Aggregator der Policy-Schicht.

Bündelt pro Operation die zutreffenden Regeln und sammelt ALLE Violations, damit
der Aufrufer dem Nutzer jeden Verstoß auf einmal zeigen kann. Der Service ruft
eine dieser Funktionen, prüft mit ``has_blockers`` und übersetzt Blocker in einen
HTTP-Fehler (409/422); Warnungen reicht er als Hinweise durch.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime

from zeiterfassung.policy import absence, timekeeping
from zeiterfassung.policy.context import AbsenceEntry, BreakInterval
from zeiterfassung.policy.violations import Violation


def has_blockers(violations: Iterable[Violation]) -> bool:
    """True, wenn mindestens eine blockierende Violation dabei ist."""
    return any(v.is_block for v in violations)


def check_new_absence(
    new: AbsenceEntry,
    existing: Iterable[AbsenceEntry],
    *,
    remaining_hours_after: float | None = None,
) -> list[Violation]:
    """Regeln beim Anlegen einer Abwesenheit (A3 Überschneidung + A9 Saldo-Warnung)."""
    out: list[Violation] = []
    if (v := absence.check_no_overlap(new, existing)) is not None:
        out.append(v)
    if remaining_hours_after is not None:
        if (v := absence.check_balance_warning(remaining_hours_after)) is not None:
            out.append(v)
    return out


def check_new_break(
    new: BreakInterval,
    *,
    work_start: datetime | None,
    work_end: datetime | None,
    existing: Iterable[BreakInterval] = (),
) -> list[Violation]:
    """Regeln beim Eintragen/Korrigieren einer Pause (C7 + B-neu-1 + B-neu-2)."""
    return timekeeping.check_break(new, work_start=work_start, work_end=work_end, existing=existing)


def check_work_entry(
    work_date: date,
    absences: Iterable[AbsenceEntry],
) -> list[Violation]:
    """Regeln beim Erfassen von Arbeitszeit an einem Tag (Krank-Konflikt)."""
    out: list[Violation] = []
    if (v := timekeeping.check_work_allowed_on_date(work_date, absences)) is not None:
        out.append(v)
    return out
