"""Domäne B — Stempeln / Pausen / Korrekturen.

Die Live-Stempel-Regeln (State-Machine, Rate-Limit, Terminal-Undo) bleiben im
``clock_service`` (sie brauchen Reihenfolge/Zeitpunkt des letzten Events). Hier
stehen die *Pausen-Konfliktregeln*, die bisher fehlten und v.a. bei manuellen/
korrigierten Pausen greifen (im Live-Fluss verhindert die State-Machine
Verschachtelung ohnehin).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime

from zeiterfassung.policy.context import LIVING_STATUSES, AbsenceEntry, BreakInterval
from zeiterfassung.policy.violations import Violation


def check_work_allowed_on_date(
    work_date: date,
    absences: Iterable[AbsenceEntry],
) -> Violation | None:
    """── Kein Arbeitszeit-Eintrag an einem als Krank markierten Tag ──

    Steht für den Tag ein noch "lebender" Krank-Antrag (SICK_LEAVE), ist das
    Erfassen von Arbeitszeit (Kommt/Geht/Pause) ein Konflikt und wird blockiert:
    krank und gleichzeitig arbeiten am selben Tag passt nicht zusammen. Andere
    Abwesenheiten (z.B. Urlaub) blocken hier bewusst NICHT — dort genügt eine
    Warnung (physisch anwesend trotz Urlaub ist möglich).
    """
    for a in absences:
        if a.status is not None and a.status not in LIVING_STATUSES:
            continue
        if a.request_type != "SICK_LEAVE":
            continue
        if a.date_from <= work_date <= a.date_to:
            return Violation.block(
                "timekeeping.on_sick_day",
                f"Der {work_date:%d.%m.%Y} ist als Krank eingetragen — "
                f"hier kann keine Arbeitszeit erfasst werden.",
                sick_from=a.date_from.isoformat(),
                sick_to=a.date_to.isoformat(),
            )
    return None


def check_break(
    new: BreakInterval,
    *,
    work_start: datetime | None,
    work_end: datetime | None,
    existing: Iterable[BreakInterval] = (),
) -> list[Violation]:
    """Alle Pausen-Regeln für eine neue/korrigierte Pause.

    Args:
        new:        die einzutragende Pause (Von/Bis).
        work_start: erstes Kommt des Tages (Beginn der Arbeitszeit).
        work_end:   letztes Geht des Tages; ``None`` = Schicht noch offen.
        existing:   bereits erfasste Pausen desselben Tages.

    Gibt eine Liste aller gefundenen Verstöße zurück (kann leer sein).
    Die Pause wird am tatsächlichen Kommt–Geht-Fenster gemessen, nicht an der
    Wanduhr — so lassen sich Arbeitszeiten auch für **zukünftige** Tage eintragen.
    """
    violations: list[Violation] = []

    # tz-Awareness vereinheitlichen: alle Zeiten betreffen denselben (UTC-)
    # Kalendertag. In Produktion sind alle aware, aus SQLite gelesene Werte sind
    # naiv — für den Wanduhr-Vergleich auf tz-frei normalisieren, damit kein
    # naive/aware-Vergleichsfehler entsteht.
    def _n(dt: datetime | None) -> datetime | None:
        return dt.replace(tzinfo=None) if dt is not None else None

    n_start, n_end = _n(new.start), _n(new.end)
    n_work_start, n_work_end = _n(work_start), _n(work_end)

    # ── R-C7: Pausenende muss nach Pausenbeginn liegen ──────────────────────
    if new.end <= new.start:
        violations.append(
            Violation.block(
                "break.end_before_start",
                "Pausenende muss nach dem Pausenbeginn liegen.",
            )
        )
        return violations  # weitere Prüfungen sind bei kaputtem Intervall sinnlos

    # ── B-neu-1: Pause muss innerhalb der Arbeitszeit liegen ────────────────
    # Arbeitszeit = erstes Kommt … letztes Geht. Ohne Kommt gibt es keine
    # Arbeitszeit, in die die Pause fallen könnte. Bei offener Schicht (noch kein
    # Geht) gilt nur die Untergrenze (nach dem Kommt) — nach oben ist die Pause
    # bewusst NICHT durch die aktuelle Uhrzeit begrenzt, damit Arbeitszeiten auch
    # für zukünftige Tage eingetragen werden können.
    if (
        n_work_start is None
        or n_start < n_work_start
        or (n_work_end is not None and n_end > n_work_end)
    ):
        violations.append(
            Violation.block(
                "break.outside_work",
                "Die Pause muss innerhalb der Arbeitszeit (Kommt–Geht) liegen.",
                work_start=n_work_start.isoformat() if n_work_start else None,
                work_end=n_work_end.isoformat() if n_work_end else None,
            )
        )

    # ── B-neu-2: Pausen dürfen sich nicht überschneiden ─────────────────────
    for b in existing:
        b_start, b_end = _n(b.start), _n(b.end)
        # Zwei Intervalle überlappen, wenn jedes vor dem Ende des anderen beginnt.
        if n_start < b_end and b_start < n_end:
            violations.append(
                Violation.block(
                    "break.overlap",
                    f"Überschneidet die bestehende Pause {b_start:%H:%M}–{b_end:%H:%M}.",
                    conflict_start=b_start.isoformat(),
                    conflict_end=b_end.isoformat(),
                )
            )
            break  # eine Überschneidungsmeldung reicht

    # ── B-neu-3: Pausen dürfen die Arbeitszeit nicht komplett aufzehren ──────
    # Bei geschlossener Schicht (Kommt und Geht bekannt) muss Netto-Arbeitszeit
    # übrig bleiben — eine Pause über die ganze Schicht (0 h Arbeit) ist sinnlos.
    if n_work_start is not None and n_work_end is not None:
        window = (n_work_end - n_work_start).total_seconds()
        total_break = (n_end - n_start).total_seconds()
        for b in existing:
            bs, be = _n(b.start), _n(b.end)
            if bs is not None and be is not None:
                total_break += (be - bs).total_seconds()
        if total_break >= window:
            violations.append(
                Violation.block(
                    "break.consumes_all_work",
                    "Die Pause darf nicht die gesamte Arbeitszeit ausfüllen — "
                    "es muss Arbeitszeit übrig bleiben.",
                )
            )

    return violations


def check_punch_order(punches: list[tuple[bool, datetime]]) -> list[Violation]:
    """Kommt/Geht müssen plausibel sein: jedes Geht liegt nach seinem Kommt.

    ``punches``: Liste ``(is_kommt, zeit)`` NUR der Kommt/Geht-Events des Tages, bereits
    nach Zeit sortiert (Pausen bleiben außen vor). Fängt Unsinn ab wie Kommt 08:00 /
    Geht 07:00 oder ein Geht ohne vorheriges Kommt.
    """

    def _n(dt: datetime) -> datetime:
        return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt

    violations: list[Violation] = []
    state_in = False
    last_in: datetime | None = None
    for is_kommt, t in punches:
        tt = _n(t)
        if is_kommt:
            if state_in:
                violations.append(
                    Violation.block("punch.double_in", "Doppeltes Kommt ohne Geht dazwischen.")
                )
                return violations
            state_in = True
            last_in = tt
        else:
            if not state_in or (last_in is not None and tt <= last_in):
                violations.append(
                    Violation.block("punch.out_before_in", "Kommt muss vor Geht liegen.")
                )
                return violations
            state_in = False
    return violations
