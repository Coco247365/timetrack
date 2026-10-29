"""German public holiday engine.

Calculates all Feiertage for any year and Bundesland using the Gaussian
Easter algorithm plus fixed and moveable feast offsets.

Bundesland codes follow ISO 3166-2:DE (without the "DE-" prefix):
    BB, BE, BW, BY, HB, HE, HH, MV, NI, NW, RP, SH, SL, SN, ST, TH
"""

from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache

# ---------------------------------------------------------------------------
# Easter (Gaussian algorithm — Meeus/Jones/Butcher)
# ---------------------------------------------------------------------------


def _easter_sunday(year: int) -> date:
    """Return the date of Easter Sunday for *year* using the Butcher algorithm."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    ll = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ll) // 451
    month = (h + ll - 7 * m + 114) // 31
    day = ((h + ll - 7 * m + 114) % 31) + 1
    return date(year, month, day)


# ---------------------------------------------------------------------------
# Holidays by Bundesland
# ---------------------------------------------------------------------------

# Holidays that exist only in certain Bundeslaender:
#   key = internal name  value = set of Bundesland codes where it applies
_REGIONAL_HOLIDAYS: dict[str, set[str]] = {
    # Heilige Drei Koenige — 6 Jan
    "heilige_drei_koenige": {"BW", "BY", "ST"},
    # Frauentag — 8 Mar (Berlin since 2019, MV since 2023)
    "frauentag": {"BE", "MV"},
    # Gruendonnerstag — Thu before Easter (none, listed for completeness)
    # Fronleichnam — 60 days after Easter
    "fronleichnam": {"BW", "BY", "HE", "NW", "RP", "SL", "SN", "TH"},
    # Mariä Himmelfahrt — 15 Aug
    "maria_himmelfahrt": {"BY", "SL"},
    # Weltkindertag — 20 Sep (Thüringen)
    "weltkindertag": {"TH"},
    # Reformationstag — 31 Oct (permanent in BB, MV, SN, ST, TH; HB, HH, NI, SH since 2018)
    "reformationstag": {"BB", "HB", "HH", "MV", "NI", "SH", "SN", "ST", "TH"},
    # Allerheiligen — 1 Nov
    "allerheiligen": {"BW", "BY", "NW", "RP", "SL"},
    # Buß- und Bettag — Wed before 23 Nov (Sachsen only)
    "buss_und_bettag": {"SN"},
}


def _buss_und_bettag(year: int) -> date:
    """Return Buß- und Bettag: the Wednesday before 23 November."""
    nov23 = date(year, 11, 23)
    # weekday(): Monday=0 … Sunday=6; Wednesday=2
    days_back = (nov23.weekday() - 2) % 7
    return nov23 - timedelta(days=days_back)


def _frauentag_applies(year: int, bundesland: str) -> bool:
    """Berlin: since 2019. MV: since 2023."""
    if bundesland == "BE" and year >= 2019:
        return True
    if bundesland == "MV" and year >= 2023:
        return True
    return False


@lru_cache(maxsize=256)
def get_holidays(year: int, bundesland: str = "HH") -> dict[date, str]:
    """Return all public holidays for *year* in *bundesland*.

    Returns a mapping of ``{date: holiday_name_german}``.
    """
    bl = bundesland.upper()
    easter = _easter_sunday(year)

    # --- Bundesweite Feiertage -----------------------------------------------
    holidays: dict[date, str] = {
        date(year, 1, 1): "Neujahr",
        easter - timedelta(days=2): "Karfreitag",
        easter: "Ostersonntag",
        easter + timedelta(days=1): "Ostermontag",
        date(year, 5, 1): "Tag der Arbeit",
        easter + timedelta(days=39): "Christi Himmelfahrt",
        easter + timedelta(days=49): "Pfingstsonntag",
        easter + timedelta(days=50): "Pfingstmontag",
        date(year, 10, 3): "Tag der Deutschen Einheit",
        date(year, 12, 25): "1. Weihnachtstag",
        date(year, 12, 26): "2. Weihnachtstag",
    }

    # --- Regionale Feiertage ------------------------------------------------
    if bl in _REGIONAL_HOLIDAYS["heilige_drei_koenige"]:
        holidays[date(year, 1, 6)] = "Heilige Drei Koenige"

    if _frauentag_applies(year, bl):
        holidays[date(year, 3, 8)] = "Internationaler Frauentag"

    if bl in _REGIONAL_HOLIDAYS["fronleichnam"]:
        holidays[easter + timedelta(days=60)] = "Fronleichnam"

    if bl in _REGIONAL_HOLIDAYS["maria_himmelfahrt"]:
        holidays[date(year, 8, 15)] = "Maria Himmelfahrt"

    if bl in _REGIONAL_HOLIDAYS["weltkindertag"]:
        holidays[date(year, 9, 20)] = "Weltkindertag"

    if bl in _REGIONAL_HOLIDAYS["reformationstag"]:
        holidays[date(year, 10, 31)] = "Reformationstag"

    if bl in _REGIONAL_HOLIDAYS["allerheiligen"]:
        holidays[date(year, 11, 1)] = "Allerheiligen"

    if bl in _REGIONAL_HOLIDAYS["buss_und_bettag"]:
        holidays[_buss_und_bettag(year)] = "Buss- und Bettag"

    return holidays


def is_holiday(d: date, bundesland: str = "HH") -> bool:
    """Return True if *d* is a public holiday in *bundesland*."""
    return d in get_holidays(d.year, bundesland)


def is_sunday(d: date) -> bool:
    """Return True if *d* falls on a Sunday."""
    return d.weekday() == 6


def is_weekend(d: date) -> bool:
    """Return True if *d* falls on Saturday or Sunday."""
    return d.weekday() >= 5


def working_days_between(start: date, end: date, bundesland: str = "HH") -> int:
    """Count working days (Mon-Fri, excluding holidays) between start and end inclusive."""
    total = 0
    current = start
    one_day = timedelta(days=1)
    while current <= end:
        if not is_weekend(current) and not is_holiday(current, bundesland):
            total += 1
        current += one_day
    return total
