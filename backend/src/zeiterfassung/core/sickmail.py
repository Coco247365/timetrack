"""Krankmeldungs-Parser: Zeitraum aus freiem Mail-Text ziehen.

Bewusst OHNE KI: Absender und Eingangsdatum liefern die Kernfakten (wer, ab
wann), hier wird nur das optionale "bis wann" aus den ueblichen deutschen
Formulierungen extrahiert. Kein Treffer ist KEIN Fehler — dann gilt der
Standard (1 Tag ab Eingang) und HR korrigiert beim Bestaetigen.

Pure Funktionen, keine Seiteneffekte — analog zur policy/-Schicht.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

_WEEKDAYS = {
    "montag": 0,
    "dienstag": 1,
    "mittwoch": 2,
    "donnerstag": 3,
    "freitag": 4,
    "samstag": 5,
    "sonnabend": 5,
    "sonntag": 6,
}

# dd.mm. oder dd.mm.yyyy / dd.mm.yy
_DATE_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4}|\d{2})?\b")
# "bis (einschliesslich/einschl./zum) <Datum|Wochentag>"
_BIS_RE = re.compile(
    r"bis\s+(?:einschlie(?:ss|ß)lich\s+|einschl\.\s*|zum\s+)?"
    r"(?:(\d{1,2})\.(\d{1,2})\.(\d{4}|\d{2})?|([a-zA-ZäöüÄÖÜ]+))",
    re.IGNORECASE,
)
# "die naechsten N Tage" / "N Tage"
_TAGE_RE = re.compile(r"\b(\d{1,2})\s+tage\b", re.IGNORECASE)
_WOCHE_RE = re.compile(r"\bdiese woche\b|\bden rest der woche\b|\brestliche woche\b", re.IGNORECASE)
_MORGEN_RE = re.compile(r"\bbis morgen\b|\bnur morgen\b", re.IGNORECASE)
_HEUTE_RE = re.compile(r"\bnur heute\b|\bheute nicht\b", re.IGNORECASE)
# Gruss "Guten Morgen" darf nicht als "morgen krank" gelesen werden.
_GUTEN_MORGEN_RE = re.compile(r"guten\s+morgen", re.IGNORECASE)
_UEBERMORGEN_RE = re.compile(r"\b(?:ü|ue)bermorgen\b", re.IGNORECASE)
_MORGEN_WORT_RE = re.compile(r"\bmorgen\b", re.IGNORECASE)
_HEUTE_WORT_RE = re.compile(r"\bheute\b", re.IGNORECASE)

# Ab dieser Stunde (Lokalzeit) gilt eine Meldung ohne Datumsangabe fuer den
# NAECHSTEN Tag: wer abends "bin krank" schreibt, meint morgen.
EVENING_HOUR = 17


@dataclass(frozen=True)
class ParsedSickPeriod:
    """Ergebnis des Parsers.

    ``matched`` beschreibt, welches Muster gezogen hat (fuer die Anzeige
    "erkannt aus: 'bis 12.07.'"); None = Standardannahme 1 Tag.
    """

    date_from: date
    date_to: date
    matched: str | None


def _resolve_date(day: str, month: str, year: str | None, anchor: date) -> date | None:
    """dd.mm.[yy[yy]] in ein Datum aufloesen; ohne Jahr: nachestes Vorkommen ab anchor."""
    try:
        d, m = int(day), int(month)
        if year:
            y = int(year)
            if y < 100:
                y += 2000
            return date(y, m, d)
        candidate = date(anchor.year, m, d)
        if candidate < anchor:
            # "bis 03.01." im Dezember meint das naechste Jahr.
            candidate = date(anchor.year + 1, m, d)
        return candidate
    except ValueError:
        return None


def _next_weekday(name: str, anchor: date) -> date | None:
    wd = _WEEKDAYS.get(name.lower())
    if wd is None:
        return None
    delta = (wd - anchor.weekday()) % 7
    return anchor + timedelta(
        days=delta
    )  # heute zaehlt mit ("bis Freitag" an einem Freitag = heute)


def parse_sick_period(text: str, received: date, hour: int = 9) -> ParsedSickPeriod:
    """Zeitraum einer Krankmeldung aus Betreff+Text bestimmen.

    ``received``/``hour`` sind Eingangsdatum und -stunde (Lokalzeit).
    Grundregeln:
    - "morgen"/"übermorgen" (auch kombiniert) werden als Starttag verstanden.
    - Ohne jede Zeitangabe gilt: morgens eingegangen = ab heute, ab
      ``EVENING_HOUR`` Uhr = ab morgen (wer abends schreibt, meint morgen).
    - "heute" im Text schaltet die Abend-Regel ab.
    """
    text = _GUTEN_MORGEN_RE.sub(" ", text or "")

    has_ueber = _UEBERMORGEN_RE.search(text)
    text_wo_ueber = _UEBERMORGEN_RE.sub(" ", text)
    # "bis morgen"/"nur morgen" ist ein eigener Fall (heute UND morgen krank).
    bis_morgen = _MORGEN_RE.search(text)
    has_morgen = _MORGEN_WORT_RE.search(text_wo_ueber) and not bis_morgen
    has_heute = _HEUTE_WORT_RE.search(text)

    # --- Explizite morgen/übermorgen-Angaben ---
    if has_morgen and has_ueber:
        return ParsedSickPeriod(
            received + timedelta(days=1), received + timedelta(days=2), "morgen und übermorgen"
        )
    if has_ueber:
        return ParsedSickPeriod(
            received + timedelta(days=2), received + timedelta(days=2), "übermorgen"
        )

    # Starttag: explizit morgen > explizit heute > Abend-Regel > heute.
    if has_morgen:
        start = received + timedelta(days=1)
        start_hint = "morgen"
    elif has_heute:
        start = received
        start_hint = None
    elif hour >= EVENING_HOUR:
        start = received + timedelta(days=1)
        start_hint = f"Eingang {hour} Uhr → ab morgen"
    else:
        start = received
        start_hint = None

    def _hint(m: str) -> str:
        return f"{start_hint}, {m}" if start_hint else m

    # 1) "bis [einschliesslich] <Datum oder Wochentag>"
    m = _BIS_RE.search(text)
    if m:
        if m.group(1):
            end = _resolve_date(m.group(1), m.group(2), m.group(3), received)
            if end and end >= start:
                return ParsedSickPeriod(start, end, _hint(m.group(0)))
        elif m.group(4):
            end = _next_weekday(m.group(4), start)
            if end:
                return ParsedSickPeriod(start, end, _hint(m.group(0)))

    # 2) explizites Datum irgendwo im Text
    m = _DATE_RE.search(text)
    if m:
        end = _resolve_date(m.group(1), m.group(2), m.group(3), received)
        if end and start <= end <= received + timedelta(days=60):
            return ParsedSickPeriod(start, end, _hint(m.group(0)))

    # 3) "N Tage" — ab dem Starttag gerechnet
    m = _TAGE_RE.search(text)
    if m:
        n = int(m.group(1))
        if 1 <= n <= 60:
            return ParsedSickPeriod(start, start + timedelta(days=n - 1), _hint(m.group(0)))

    # 4) "diese Woche" -> bis Freitag dieser Woche
    m = _WOCHE_RE.search(text)
    if m:
        friday = start + timedelta(days=max(0, 4 - start.weekday()))
        return ParsedSickPeriod(start, max(friday, start), _hint(m.group(0)))

    # 5) "bis morgen" / "nur heute"
    if bis_morgen:
        return ParsedSickPeriod(received, received + timedelta(days=1), bis_morgen.group(0))
    m = _HEUTE_RE.search(text)
    if m:
        return ParsedSickPeriod(received, received, m.group(0))

    # Standard: 1 Tag ab Starttag (inkl. Abend-Regel) — HR korrigiert notfalls.
    return ParsedSickPeriod(start, start, start_hint)
