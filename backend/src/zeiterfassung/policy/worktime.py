"""Arbeitszeit-Profile — Rechenkern ohne DB/HTTP (Spec 2026-08-14).

Kind 'nfc' (Stempler): EIN Stempel-Fenster ``clock_start``–``clock_end`` in
lokaler Mandanten-Zeit; Stempel ausserhalb werden fuer die BEWERTUNG auf die
naechste Fenstergrenze geklemmt. Die Roh-Events bleiben unangetastet
(append-only, BetrVG §87) — geklemmt wird nur die Projektion (Tagesbogen,
Export, Stundenzettel, ``counted_time`` der Tagesansicht). Das Fenster darf
ueber Mitternacht gehen (``end < start`` ⇒ Nachtschicht, z. B. 22:00–06:15);
NULL auf einer Seite = keine Kappung an dieser Seite.

Kind 'auto' (Auto-Erfassung): Wochentags-Zeiten ``weekday_times`` bestimmen
Auto-Fuellung, Tages-Soll und Anwesenheits-Fenster ("Profil gewinnt komplett").
Fehlender/None-Tag = frei (Soll 0, KEIN Fallback auf den 07:00-Default);
``weekday_times`` darf komplett None sein (= ueberall frei).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from zeiterfassung.policy.statutory import effective_break_minutes


def parse_hhmm(value: str | None) -> int | None:
    """ "HH:MM" (lokale Mandanten-Zeit) -> Minuten seit Mitternacht.

    Leer/None -> None (keine Grenze an dieser Seite).
    """
    if not value:
        return None
    h_str, m_str = value.split(":")
    return int(h_str) * 60 + int(m_str)


@dataclass(frozen=True)
class Window:
    """Erlaubtes Stempel-Fenster in Minuten seit lokaler Mitternacht.

    ``None`` auf einer Seite heisst: diese Seite kappt nicht. Sind beide
    Seiten gesetzt und ``end_min < start_min``, laeuft das Fenster ueber
    Mitternacht — erlaubt ist dann je Kalendertag ``[0, end] ∪ [start, 1440)``.
    """

    start_min: int | None
    end_min: int | None

    @property
    def over_midnight(self) -> bool:
        """Beide Seiten gesetzt und Ende vor Beginn (Nachtschicht-Fenster)."""
        return (
            self.start_min is not None
            and self.end_min is not None
            and self.end_min < self.start_min
        )

    @property
    def unbounded(self) -> bool:
        """Keine Seite gesetzt — das Fenster kappt nichts."""
        return self.start_min is None and self.end_min is None


def counted_minute_of_day(minute_of_day: int, w: Window) -> int:
    """Klemmt eine lokale Tagesminute ins erlaubte Fenster.

    - Normalfenster ``[start, end]``: davor -> ``start``, danach -> ``end``.
    - Ueber Mitternacht (erlaubt ``[0, end] ∪ [start, 1440)``): in der Luecke
      ``(end..start)`` entscheidet der Mittelpunkt — davor (naeher am Ende der
      Nachtschicht) -> ``end``, sonst -> ``start``.
    - Nur eine Seite gesetzt: nur diese Seite klemmt.
    """
    start, end = w.start_min, w.end_min
    if w.over_midnight:
        # start/end sind hier garantiert gesetzt (over_midnight-Definition).
        assert start is not None and end is not None
        if minute_of_day <= end or minute_of_day >= start:
            return minute_of_day
        return end if minute_of_day * 2 < end + start else start
    if start is not None and minute_of_day < start:
        return start
    if end is not None and minute_of_day > end:
        return end
    return minute_of_day


def counted_local(dt_local: datetime, w: Window) -> datetime:
    """Lokale (Wand-)Zeit ins Fenster des SELBEN lokalen Kalendertags geklemmt.

    Liegt die Minute im Fenster, kommt ``dt_local`` unveraendert zurueck
    (inklusive Sekunden); sonst die Grenzminute des gleichen Tages. Funktioniert
    mit naiven und tz-aware Datetimes (es zaehlt nur die Wanduhr-Minute).
    """
    minute = dt_local.hour * 60 + dt_local.minute
    counted = counted_minute_of_day(minute, w)
    if counted == minute:
        return dt_local
    return dt_local.replace(hour=counted // 60, minute=counted % 60, second=0, microsecond=0)


def _is_active_kind(profile: object | None, kind: str) -> bool:
    """Aktives Profil der gewuenschten Art? (DB-frei, duck-typed ORM-Objekt)."""
    if profile is None:
        return False
    raw = getattr(profile, "kind", None)
    kind_value = getattr(raw, "value", raw)
    return kind_value == kind and bool(getattr(profile, "is_active", False))


def nfc_window(profile: object | None) -> Window | None:
    """Kappungs-Fenster eines AKTIVEN 'nfc'-Profils; sonst ``None``.

    Nimmt jedes Objekt mit ``kind``/``is_active``/``clock_start``/``clock_end``
    (typischerweise das ORM-Modell ``WorkTimeProfile``), bleibt aber DB-frei.
    Inaktive Profile, 'auto'-Profile und Profile ohne gesetzte Grenze kappen
    nicht (``None`` -> Fallback-Kette des Aufrufers, z. B. Lohnprofil-Regel).
    """
    if not _is_active_kind(profile, "nfc"):
        return None
    window = Window(
        start_min=parse_hhmm(getattr(profile, "clock_start", None)),
        end_min=parse_hhmm(getattr(profile, "clock_end", None)),
    )
    return None if window.unbounded else window


def nfc_target_minutes(profile: object | None, weekday: int) -> int | None:
    """Tages-Soll laut aktivem, stundengebundenem 'nfc'-Profil; ``None`` =
    Profil greift nicht (Nachtrag 14.08.).

    ``None`` bei: kein Profil / inaktiv / kind != 'nfc' / Bestandsprofil ohne
    ``contracted_hours_per_week`` (-> bisherige Soll-Kette aus User-Plan bzw.
    Wochenstunden ÷ 5). Greift das Profil, gewinnt es komplett: ein im
    ``weekday_target_minutes``-Dict fehlender Tag ist ``0`` (frei, kein
    Fallback) — spiegelbildlich zu :func:`auto_target_minutes`.
    """
    if not _is_active_kind(profile, "nfc"):
        return None
    if getattr(profile, "contracted_hours_per_week", None) is None:
        return None
    times = getattr(profile, "weekday_target_minutes", None) or {}
    return max(0, int(times.get(str(weekday), 0) or 0))


def fmt_hours(hours: Decimal) -> str:
    """Stunden fuer deutsche Meldungen: ``40.00`` -> "40", ``38.50`` -> "38,5"."""
    text = format(hours.normalize(), "f")
    return text.replace(".", ",")


# ---------------------------------------------------------------------------
# Kind 'auto': Wochentags-Zeiten -> Auto-Fuellung, Tages-Soll, Anwesenheit
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AutoDay:
    """Arbeitszeiten EINES Wochentags aus einem 'auto'-Profil.

    Alle Werte sind Minuten seit lokaler Mandanten-Mitternacht; die
    CRUD-Validierung garantiert ``start < end`` (kein Ueber-Mitternacht bei
    Auto-Profilen) und ein Pausenfenster innerhalb von ``[start, end]``.
    """

    start_min: int
    end_min: int
    break_start_min: int | None = None
    break_end_min: int | None = None

    @property
    def presence_minutes(self) -> int:
        """Anwesenheits-Fenster (Kommt bis Geht) in Minuten."""
        return max(0, self.end_min - self.start_min)

    @property
    def break_window_minutes(self) -> int:
        """Minuten des festen Pausenfensters (0 = kein Pausenfenster)."""
        if self.break_start_min is None or self.break_end_min is None:
            return 0
        return max(0, self.break_end_min - self.break_start_min)

    @property
    def break_minutes(self) -> int:
        """Effektive Tages-Pause: Pausenfenster, mindestens die gesetzliche
        Pflichtpause (ArbZG §4 via ``effective_break_minutes``) — ein Profil
        ohne (zu kurzes) Pausenfenster hebelt das Gesetz nicht aus."""
        return effective_break_minutes(self.presence_minutes, self.break_window_minutes)

    @property
    def target_minutes(self) -> int:
        """Tages-Soll = Fenster − effektive Pause.

        Bewusst mit der GESETZLICHEN Mindestpause gerechnet (nicht nur dem
        Pausenfenster), damit Soll und Auto-Fuellung dieselbe Zahl ergeben —
        sonst liefe jeder Profil-Tag ohne Pausenfenster auf ein Dauer-Minus.
        """
        return max(0, self.presence_minutes - self.break_minutes)


def auto_applies(profile: object | None) -> bool:
    """Greift dieses Profil als Auto-Profil? (aktiv und kind='auto').

    ``False`` -> bisheriges Verhalten der Aufrufer (07:00-Default, Soll aus
    Wochenstunden/Wochentagsplan) — spiegelbildlich zu :func:`nfc_window`,
    das fuer 'auto'-Profile ``None`` liefert.
    """
    return _is_active_kind(profile, "auto")


def auto_day(profile: object | None, weekday: int) -> AutoDay | None:
    """Zeiten des Wochentags (0=Mo..6=So) eines AKTIVEN 'auto'-Profils.

    ``None`` wenn das Profil nicht greift (kein Profil / inaktiv /
    kind != 'auto') ODER der Tag laut Profil frei ist. Ob "frei" oder
    "kein Profil" unterscheidet der Aufrufer via :func:`auto_target_minutes`.
    """
    if not _is_active_kind(profile, "auto"):
        return None
    times = getattr(profile, "weekday_times", None) or {}
    return _auto_day_from_dict(times.get(str(weekday)))


def _auto_day_from_dict(day: dict | None) -> AutoDay | None:
    """Roh-Dict eines Wochentags (``weekday_times``-Eintrag) -> :class:`AutoDay`."""
    if not day:
        return None
    start = parse_hhmm(day.get("start"))
    end = parse_hhmm(day.get("end"))
    if start is None or end is None:
        return None
    return AutoDay(
        start_min=start,
        end_min=end,
        break_start_min=parse_hhmm(day.get("break_start")),
        break_end_min=parse_hhmm(day.get("break_end")),
    )


def auto_target_minutes(profile: object | None, weekday: int) -> int | None:
    """Tages-Soll laut aktivem 'auto'-Profil; ``None`` = Profil greift nicht.

    ``None`` nur bei kein Profil / inaktiv / kind != 'auto' (-> bisherige
    Soll-Kette aus Wochenstunden/Wochentagsplan). Greift das Profil, gewinnt
    es KOMPLETT: ein freier Profil-Tag ist ``0`` (kein Minus, kein Fallback).
    """
    if not _is_active_kind(profile, "auto"):
        return None
    day = auto_day(profile, weekday)
    return 0 if day is None else day.target_minutes


def auto_contracted_hours_per_week(weekday_times: dict | None) -> Decimal | None:
    """Vertragsstunden eines 'auto'-Profils, BERECHNET aus ``weekday_times``.

    Summe der Tagessolls (:attr:`AutoDay.target_minutes`, also inkl.
    Mindestpausen-Logik) ueber alle Wochentage, in Stunden mit 2 Nachkommastellen
    (Nachtrag 14.08.). ``None`` bei leerem/fehlendem ``weekday_times`` — so ein
    Profil ("alles frei") bindet NICHT an die Vertragsstunden des Mitarbeiters.
    Bewusst ohne is_active/kind-Pruefung: auch inaktive Profile zeigen ihre
    berechneten Stunden in der API-Response.
    """
    if not weekday_times:
        return None
    total = 0
    for weekday in range(7):
        day = _auto_day_from_dict(weekday_times.get(str(weekday)))
        if day is not None:
            total += day.target_minutes
    return (Decimal(total) / Decimal(60)).quantize(Decimal("0.01"))
