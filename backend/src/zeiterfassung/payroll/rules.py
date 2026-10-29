"""Reine Berechnungslogik für die Lohnarten-Zuordnung.

Alle Funktionen sind deterministisch und ohne DB/HTTP testbar: der Aufrufer
liefert Events, Segmente, Feiertage und Konfiguration als schlichte Werte.
Zeitstempel sind naive UTC; die lokale Sicht entsteht über ``tz_offset_min``
(Offset in Minuten zu UTC, z.B. 120 für CEST).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from zeiterfassung.policy.worktime import Window, counted_local

# Event-Typen wie in services/clock_service.py (State-Machine der Stempeluhr).
_PUNCH_IN = "PUNCH_IN"
_PUNCH_OUT = "PUNCH_OUT"
_BREAK_START = "BREAK_START"
_BREAK_END = "BREAK_END"


@dataclass(frozen=True)
class SurchargeConfig:
    """Konfiguration der Zuschlagsfenster (Stunden 0–23, lokale Zeit).

    ``saturday_from``/``sunday_from`` legen fest, ab welcher Uhrzeit die
    Sa/So-Kategorie greift; Arbeit davor zählt als normale Arbeitszeit.
    """

    night_start: int = 23
    night_end: int = 6
    saturday_from: int = 0
    sunday_from: int = 0


@dataclass(frozen=True)
class WorkSegment:
    """Ein abgeschlossenes Arbeits-Intervall (naive UTC, Pausen ausgenommen)."""

    start: datetime
    end: datetime


def segments_from_events(
    events: list[tuple[str, datetime]],
) -> list[WorkSegment]:
    """Baut abgeschlossene Arbeits-Segmente aus chronologischen Stempel-Events.

    Regel: PUNCH_IN öffnet Arbeit, BREAK_START/PUNCH_OUT schließen das
    laufende Segment, BREAK_END öffnet es wieder; ein am Ende noch offenes
    Segment wird IGNORIERT (der Export rechnet nur abgeschlossene Arbeit).
    """
    segments: list[WorkSegment] = []
    open_start: datetime | None = None

    for event_type, event_time in events:
        if event_type == _PUNCH_IN:
            if open_start is None:
                open_start = event_time
        elif event_type == _BREAK_END:
            if open_start is None:
                open_start = event_time
        elif event_type in (_BREAK_START, _PUNCH_OUT):
            if open_start is not None and event_time > open_start:
                segments.append(WorkSegment(start=open_start, end=event_time))
            open_start = None

    # open_start != None hier = offenes Segment am Ende → bewusst verworfen.
    return segments


def _minutes(delta: timedelta) -> int:
    """Volle Minuten eines (nicht-negativen) Zeitraums."""
    return int(delta.total_seconds() // 60)


def _recurring_window_overlap(
    start_local: datetime,
    end_local: datetime,
    window_start_h: int,
    window_end_h: int,
) -> int:
    """Minuten-Überlappung eines lokalen Intervalls mit einem täglichen Fenster.

    Regel: das Fenster wiederholt sich jeden Tag und darf über Mitternacht
    gehen (z.B. 23→6); ``start == end`` bedeutet ein leeres Fenster.
    """
    if start_local >= end_local or window_start_h == window_end_h:
        return 0

    total = 0
    # Fensterinstanzen aller berührten Tage prüfen (Vortag wegen Mitternacht-
    # Überhang mitnehmen).
    day = start_local.date() - timedelta(days=1)
    last_day = end_local.date()
    while day <= last_day:
        day_start = datetime(day.year, day.month, day.day)
        w_start = day_start + timedelta(hours=window_start_h)
        if window_start_h < window_end_h:
            w_end = day_start + timedelta(hours=window_end_h)
        else:  # über Mitternacht: Ende liegt am Folgetag
            w_end = day_start + timedelta(days=1, hours=window_end_h)
        lo = max(start_local, w_start)
        hi = min(end_local, w_end)
        if hi > lo:
            total += _minutes(hi - lo)
        day += timedelta(days=1)
    return total


def overlap_minutes(
    seg: WorkSegment,
    window_start_h: int,
    window_end_h: int,
    tz_offset_min: int,
) -> int:
    """Minutengenaue Überlappung eines Segments mit einem täglichen Zeitfenster.

    Regel: das Segment (naive UTC) wird per ``tz_offset_min`` in lokale Zeit
    verschoben und gegen das täglich wiederkehrende Fenster
    ``window_start_h``→``window_end_h`` geschnitten (Fenster darf über
    Mitternacht gehen).
    """
    offset = timedelta(minutes=tz_offset_min)
    return _recurring_window_overlap(
        seg.start + offset, seg.end + offset, window_start_h, window_end_h
    )


def categorize_day(
    work_date: date,
    segments: list[WorkSegment],
    *,
    is_holiday: bool,
    cfg: SurchargeConfig,
    tz_offset_min: int,
) -> dict[str, int]:
    """Minuten je Export-Kategorie für einen Kalendertag (lokale Zeit).

    Regel: die Basis-Kategorie ist genau eine von NORMALE_ARBEITSZEIT /
    SAMSTAG_ARBEIT / SONNTAG_ARBEIT / FEIERTAG_ARBEIT (Feiertag schlägt
    Sa/So; Sa/So erst ab der konfigurierten Schwelle, davor normal), und
    NACHT_ARBEIT kommt als überlappender Zuschlag ZUSÄTZLICH obendrauf.

    Nur Segment-Anteile, die lokal auf ``work_date`` fallen, werden gezählt;
    Anteile nach Mitternacht gehören zum Aufruf für den Folgetag.
    """
    offset = timedelta(minutes=tz_offset_min)
    day_start = datetime(work_date.year, work_date.month, work_date.day)
    day_end = day_start + timedelta(days=1)

    result = {
        "NORMALE_ARBEITSZEIT": 0,
        "SAMSTAG_ARBEIT": 0,
        "SONNTAG_ARBEIT": 0,
        "FEIERTAG_ARBEIT": 0,
        "NACHT_ARBEIT": 0,
    }

    weekday = work_date.weekday()  # Mo=0 … So=6
    if is_holiday:
        threshold: datetime | None = None
        surcharge_key = "FEIERTAG_ARBEIT"
    elif weekday == 5:
        threshold = day_start + timedelta(hours=cfg.saturday_from)
        surcharge_key = "SAMSTAG_ARBEIT"
    elif weekday == 6:
        threshold = day_start + timedelta(hours=cfg.sunday_from)
        surcharge_key = "SONNTAG_ARBEIT"
    else:
        threshold = None
        surcharge_key = "NORMALE_ARBEITSZEIT"

    for seg in segments:
        # Auf den lokalen Kalendertag zuschneiden.
        lo = max(seg.start + offset, day_start)
        hi = min(seg.end + offset, day_end)
        if hi <= lo:
            continue

        if surcharge_key == "FEIERTAG_ARBEIT" or threshold is None:
            result[surcharge_key] += _minutes(hi - lo)
        else:
            # Vor der Sa/So-Schwelle: normale Arbeitszeit; ab Schwelle: Zuschlag.
            before_hi = min(hi, threshold)
            if before_hi > lo:
                result["NORMALE_ARBEITSZEIT"] += _minutes(before_hi - lo)
            after_lo = max(lo, threshold)
            if hi > after_lo:
                result[surcharge_key] += _minutes(hi - after_lo)

        # Nachtzuschlag überlappt die Basis-Kategorie (zusätzlich zählen).
        result["NACHT_ARBEIT"] += _recurring_window_overlap(lo, hi, cfg.night_start, cfg.night_end)

    return result


def is_midnight_continuation(start_local: datetime) -> bool:
    """Segmentbeginn um exakt 00:00 Lokalzeit = Fortsetzung einer Nachtschicht.

    So legt der Tages-Editor eine Schicht ueber Mitternacht an (Geht 23:59 am
    Anfangstag, Kommt 00:00 am Folgetag; Spec 2026-09-15). Ein solcher Beginn
    ist keine Fruehstempelung — die Regeln "fruehester Beginn" und
    Stempel-Fenster duerfen ihn nicht nach vorn klemmen, sonst verschwindet
    der Morgenteil der Nachtschicht (Beta: 00:00 -> 06:00 = 0 h).
    """
    return start_local.hour == 0 and start_local.minute == 0


def clip_segments_earliest_start(
    segments: list[WorkSegment],
    earliest_min: int,
    tz_offset_min: int,
) -> list[WorkSegment]:
    """Kappt Segmentanfänge auf eine früheste lokale Uhrzeit (Profil-Regel).

    Nachbildung des Altsystem ("06:00 (05:19)"): Stempelzeiten vor
    ``earliest_min`` (Minuten seit Mitternacht, LOKAL) gelten ab dieser Uhrzeit
    des jeweiligen lokalen Tages. Segmente, die komplett vor der Schwelle
    liegen, entfallen; ein über Mitternacht laufendes Segment wird nur an
    seinem Start-Ende geklappt (das Ende bleibt unverändert).

    Die Original-Events bleiben unangetastet — dies ist reine Bewertungslogik
    für Export/Stundenzettel.
    """
    offset = timedelta(minutes=tz_offset_min)
    result: list[WorkSegment] = []
    for seg in segments:
        start_local = seg.start + offset
        day_start_local = datetime(start_local.year, start_local.month, start_local.day)
        threshold_local = day_start_local + timedelta(minutes=earliest_min)
        if start_local >= threshold_local or is_midnight_continuation(start_local):
            result.append(seg)
            continue
        new_start_local = threshold_local
        end_local = seg.end + offset
        if new_start_local >= end_local:
            continue  # Segment liegt komplett vor der Schwelle -> entfaellt
        result.append(WorkSegment(start=new_start_local - offset, end=seg.end))
    return result


def clip_segments_local_tz(
    segments: list[WorkSegment],
    earliest_min: int | None,
    tzinfo: object,
) -> list[WorkSegment]:
    """Bequemlichkeits-Wrapper: pro Segment den lokalen UTC-Offset ermitteln (Mittags-Trick,
    DST-sicher) und mit ``clip_segments_earliest_start`` kappen.

    ``earliest_min`` None bedeutet: keine Kappung (Segmente unveraendert zurueckgeben).
    """
    if earliest_min is None:
        return segments
    result: list[WorkSegment] = []
    for seg in segments:
        off = int(
            datetime(seg.start.year, seg.start.month, seg.start.day, 12, tzinfo=tzinfo)  # type: ignore[arg-type]
            .utcoffset()
            .total_seconds()
            // 60
        )
        result.extend(clip_segments_earliest_start([seg], earliest_min, off))
    return result


def clip_segments_window(
    segments: list[WorkSegment],
    window: Window,
    tz_offset_min: int,
) -> list[WorkSegment]:
    """Kappt Segmente auf das Stempel-Fenster eines Arbeitszeit-Profils (kind 'nfc').

    Verallgemeinerung von ``clip_segments_earliest_start`` (Spec 2026-08-14):
    BEIDE Grenzen (Beginn UND Ende, ggf. ueber Mitternacht) werden je Endpunkt
    auf dessen EIGENEM lokalen Kalendertag mit ``policy.worktime.counted_local``
    geklemmt — dieselbe Regel wie im Tagesbogen (clock_service), damit Konto,
    Stundenzettel und Export dieselbe Zeit bewerten.

    Eine Klemmung darf ein Segment nur VERKUERZEN (der Start rutscht nie nach
    vorn, das Ende nie nach hinten) — sonst wuerde z. B. ein ueber Mitternacht
    laufendes Segment bei einem Nur-Beginn-Fenster Zeit dazu erfinden. Leere
    Segmente entfallen. Die Original-Events bleiben unangetastet — dies ist
    reine Bewertungslogik.
    """
    offset = timedelta(minutes=tz_offset_min)
    result: list[WorkSegment] = []
    for seg in segments:
        start_local = seg.start + offset
        end_local = seg.end + offset
        new_start = (
            start_local
            if is_midnight_continuation(start_local)
            else max(start_local, counted_local(start_local, window))
        )
        new_end = min(end_local, counted_local(end_local, window))
        if new_end <= new_start:
            continue  # Segment liegt komplett ausserhalb des Fensters
        result.append(WorkSegment(start=new_start - offset, end=new_end - offset))
    return result


def clip_segments_window_local_tz(
    segments: list[WorkSegment],
    window: Window | None,
    tzinfo: object,
) -> list[WorkSegment]:
    """Bequemlichkeits-Wrapper wie ``clip_segments_local_tz``: pro Segment den
    lokalen UTC-Offset ermitteln (Mittags-Trick, DST-sicher) und mit
    ``clip_segments_window`` kappen.

    ``window`` None bedeutet: keine Kappung (Segmente unveraendert zurueckgeben).
    """
    if window is None:
        return segments
    result: list[WorkSegment] = []
    for seg in segments:
        off = int(
            datetime(seg.start.year, seg.start.month, seg.start.day, 12, tzinfo=tzinfo)  # type: ignore[arg-type]
            .utcoffset()
            .total_seconds()
            // 60
        )
        result.extend(clip_segments_window([seg], window, off))
    return result


def parse_earliest_start(value: str | None) -> int | None:
    """ "HH:MM" (Profil-Feld ``fruehester_beginn``) -> Minuten seit Mitternacht.

    Leer/None -> None (keine Kappung).
    """
    if not value:
        return None
    h_str, m_str = value.split(":")
    return int(h_str) * 60 + int(m_str)


def paid_holiday_minutes(
    month_holidays: list[date],
    worked_dates: set[date],
    absent_dates: set[date],
    daily_target_min: int,
) -> int:
    """FEIERTAG_BEZAHLT: Tagessoll je Mo–Fr-Feiertag ohne Arbeit und Abwesenheit.

    Regel: nur Feiertage, die auf Mo–Fr fallen, werden gutgeschrieben, und nur
    wenn an dem Tag weder gearbeitet noch Urlaub/Krank erfasst wurde (keine
    Doppel-Gutschrift).
    """
    total = 0
    for holiday in month_holidays:
        if holiday.weekday() >= 5:
            continue  # Sa/So-Feiertage lösen keine Gutschrift aus
        if holiday in worked_dates or holiday in absent_dates:
            continue
        total += daily_target_min
    return total


def absence_minutes(absence_days: float, daily_target_min: int) -> int:
    """URLAUB_STD/KRANK_STD: Abwesenheitstage × Tagessoll, halbtags-genau.

    Regel: ``absence_days`` kann 0,5-Schritte enthalten (Halbtage); das
    Ergebnis wird auf volle Minuten gerundet.
    """
    return round(absence_days * daily_target_min)
