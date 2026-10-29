"""Stundenzettel-PDF: Ausweis von gekapptem Beginn und angehobener Pause.

Das PDF muss dieselben Zusatzangaben zeigen wie die Web-Ansicht — sonst steht
im ausgedruckten bzw. per Mail versendeten Zettel eine andere Zeit als auf dem
Bildschirm. Geprueft werden die beiden Zellen-Formatierer der Tagestabelle.
"""

from __future__ import annotations

from zeiterfassung.schemas.admin_reports import TimesheetDay, TimesheetSegment
from zeiterfassung.services.timesheet_pdf import kommt_text, pause_text


def _seg(**kw) -> TimesheetSegment:
    return TimesheetSegment(start="06:00", end="14:20", **kw)


def _day(**kw) -> TimesheetDay:
    base = {
        "date": __import__("datetime").date(2026, 6, 1),
        "weekday": "Mo",
        "segments": [],
        "pause_minutes": 0,
        "ist_minutes": 0,
        "soll_minutes": 0,
        "diff_minutes": 0,
        "absence": None,
        "holiday": False,
    }
    return TimesheetDay(**{**base, **kw})


class TestKommtText:
    def test_gekappter_beginn_zeigt_stempelzeit_in_klammern(self) -> None:
        seg = _seg(start_punched="05:55", clipped_earliest_start=True)
        assert kommt_text(seg) == "06:00 (05:55)"

    def test_ohne_kappung_nur_die_gewertete_zeit(self) -> None:
        assert kommt_text(_seg()) == "06:00"

    def test_stempelzeit_ohne_kappungs_flag_wird_nicht_ausgewiesen(self) -> None:
        # Defensiv: nur das Flag entscheidet, nicht das blosse Vorhandensein.
        assert kommt_text(_seg(start_punched="05:55")) == "06:00"

    def test_kein_segment_bleibt_leer(self) -> None:
        assert kommt_text(None) == ""


class TestPauseText:
    def test_angehobene_pause_zeigt_gestempelte_in_klammern(self) -> None:
        day = _day(pause_minutes=30, pause_punched_minutes=15, pause_raised_by_law=True)
        assert pause_text(day) == "0:30 (0:15)"

    def test_ausreichend_gestempelte_pause_bleibt_schlicht(self) -> None:
        day = _day(pause_minutes=45, pause_punched_minutes=45)
        assert pause_text(day) == "0:45"

    def test_ohne_pause_bleibt_leer(self) -> None:
        assert pause_text(_day()) == ""

    def test_angehoben_von_null_gestempelt(self) -> None:
        day = _day(pause_minutes=30, pause_punched_minutes=0, pause_raised_by_law=True)
        assert pause_text(day) == "0:30 (0:00)"
