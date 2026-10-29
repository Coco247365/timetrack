"""ICS-Kalender (iCalendar) fuer Abwesenheiten — ohne Fremdbibliothek.

Erzeugt ganztaegige Termine (DTSTART/DTEND als DATE, Ende exklusiv) mit
stabilen UIDs (Antrags-Id), damit Kalender-Abos Updates statt Duplikaten
sehen. Datenschutz: Krankmeldungen werden als neutrales "Abwesend"
beschriftet, sofern der Aufrufer kein medical:read hat.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta


@dataclass(frozen=True)
class IcsEvent:
    uid: str
    summary: str
    date_from: date
    date_to: date  # inklusiv — wird intern +1 Tag (ICS-Ende ist exklusiv)
    sequence: int = 0


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def build_ics(events: list[IcsEvent], calendar_name: str = "Abwesenheiten") -> str:
    """Komplette VCALENDAR-Datei (CRLF-Zeilenenden, wie der Standard verlangt)."""
    now = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    lines: list[str] = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Zeiterfassung//Abwesenheiten//DE",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_escape(calendar_name)}",
    ]
    for e in events:
        end_exclusive = e.date_to + timedelta(days=1)
        lines += [
            "BEGIN:VEVENT",
            f"UID:{e.uid}@zeiterfassung",
            f"DTSTAMP:{now}",
            f"DTSTART;VALUE=DATE:{e.date_from.strftime('%Y%m%d')}",
            f"DTEND;VALUE=DATE:{end_exclusive.strftime('%Y%m%d')}",
            f"SUMMARY:{_escape(e.summary)}",
            f"SEQUENCE:{e.sequence}",
            "TRANSP:TRANSPARENT",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"
