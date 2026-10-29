"""ICS-Kalender-Export: Format, Ganztags-Logik, Escaping."""

from datetime import date

from zeiterfassung.core.ics import IcsEvent, build_ics


def test_ganztags_ende_exklusiv():
    out = build_ics([IcsEvent("u1", "Max — Urlaub", date(2026, 7, 10), date(2026, 7, 12))])
    assert "DTSTART;VALUE=DATE:20260710" in out
    # ICS-Ende ist exklusiv: bis-einschliesslich 12.07. => DTEND 13.07.
    assert "DTEND;VALUE=DATE:20260713" in out
    assert "UID:u1@zeiterfassung" in out
    assert out.startswith("BEGIN:VCALENDAR")
    assert out.rstrip().endswith("END:VCALENDAR")


def test_sonderzeichen_escaped():
    out = build_ics([IcsEvent("u2", "A; B, C\nD", date(2026, 1, 1), date(2026, 1, 1))])
    assert "SUMMARY:A\; B\\, C\\nD" in out


def test_crlf_zeilenenden():
    out = build_ics([])
    assert "\r\n" in out
    # Kein nacktes \n ohne \r davor (Standard verlangt durchgaengig CRLF).
    assert "\n" not in out.replace("\r\n", "")
