"""Tests für die reine Payroll-Regel-Schicht (ohne DB — reine Funktionen)."""

from __future__ import annotations

from datetime import date, datetime

from zeiterfassung.payroll import (
    SurchargeConfig,
    WorkSegment,
    absence_minutes,
    all_categories,
    categorize_day,
    get_category,
    overlap_minutes,
    paid_holiday_minutes,
    segments_from_events,
)


def _dt(day: int, hour: int, minute: int = 0, month: int = 7) -> datetime:
    return datetime(2026, month, day, hour, minute)


def _seg(day: int, h1: int, m1: int, h2: int, m2: int, month: int = 7) -> WorkSegment:
    return WorkSegment(start=_dt(day, h1, m1, month), end=_dt(day, h2, m2, month))


# ---------------------------------------------------------------------------
# Kategorien-Registry
# ---------------------------------------------------------------------------


def test_registry_has_all_codes_and_lookup():
    codes = {c.code for c in all_categories()}
    assert codes == {
        "NORMALE_ARBEITSZEIT",
        "SAMSTAG_ARBEIT",
        "SONNTAG_ARBEIT",
        "FEIERTAG_ARBEIT",
        "NACHT_ARBEIT",
        "FEIERTAG_BEZAHLT",
        "URLAUB_STD",
        "SONDERURLAUB_STD",
        "KRANK_STD",
        "KRANK_EAU_TELEFON",
        "KRANK_MIT_AU",
        "KRANK_OHNE_AU_MIT_LFZ",
        "KRANK_OHNE_AU_OHNE_LFZ",
        "MEHRARBEIT_0",
        "MEHRARBEIT_ZUSCHLAG",
        "MINUSSTUNDEN",
        "UEBERSTUNDEN_AUSZAHLUNG",
    }
    cat = get_category("NACHT_ARBEIT")
    assert cat.unit == "hours"
    assert cat.label  # deutsches Label vorhanden


# ---------------------------------------------------------------------------
# Segment-Bau aus Events
# ---------------------------------------------------------------------------


def test_segments_simple_session():
    events = [("PUNCH_IN", _dt(1, 8)), ("PUNCH_OUT", _dt(1, 12))]
    assert segments_from_events(events) == [WorkSegment(start=_dt(1, 8), end=_dt(1, 12))]


def test_segments_with_break():
    events = [
        ("PUNCH_IN", _dt(1, 8)),
        ("BREAK_START", _dt(1, 12)),
        ("BREAK_END", _dt(1, 12, 30)),
        ("PUNCH_OUT", _dt(1, 17)),
    ]
    assert segments_from_events(events) == [
        WorkSegment(start=_dt(1, 8), end=_dt(1, 12)),
        WorkSegment(start=_dt(1, 12, 30), end=_dt(1, 17)),
    ]


def test_segments_multiple_sessions():
    events = [
        ("PUNCH_IN", _dt(1, 6)),
        ("PUNCH_OUT", _dt(1, 10)),
        ("PUNCH_IN", _dt(1, 14)),
        ("PUNCH_OUT", _dt(1, 18)),
    ]
    assert len(segments_from_events(events)) == 2


def test_segments_open_end_ignored():
    # Offenes Segment am Ende zählt nicht — nur abgeschlossene Arbeit.
    events = [
        ("PUNCH_IN", _dt(1, 8)),
        ("PUNCH_OUT", _dt(1, 12)),
        ("PUNCH_IN", _dt(1, 13)),
    ]
    assert segments_from_events(events) == [WorkSegment(start=_dt(1, 8), end=_dt(1, 12))]
    assert segments_from_events([("PUNCH_IN", _dt(1, 8))]) == []


def test_segments_break_start_closes_segment_even_without_out():
    # BREAK_START schließt das Arbeits-Segment; die danach offene Pause
    # erzeugt keine weitere Arbeit.
    events = [("PUNCH_IN", _dt(1, 8)), ("BREAK_START", _dt(1, 11))]
    assert segments_from_events(events) == [WorkSegment(start=_dt(1, 8), end=_dt(1, 11))]


# ---------------------------------------------------------------------------
# Fenster-Überlappung
# ---------------------------------------------------------------------------


def test_overlap_window_over_midnight():
    # 22:00–07:00 gegen Nachtfenster 23→6 → 23:00–06:00 = 420 Minuten.
    seg = WorkSegment(start=_dt(1, 22), end=_dt(2, 7))
    assert overlap_minutes(seg, 23, 6, 0) == 420


def test_overlap_respects_tz_offset():
    # 21:30–22:30 UTC = 23:30–00:30 lokal (CEST, +120) → 60 Minuten Nacht.
    seg = _seg(1, 21, 30, 22, 30)
    assert overlap_minutes(seg, 23, 6, 120) == 60
    # Ohne Offset liegt dasselbe Segment komplett vor 23 Uhr.
    assert overlap_minutes(seg, 23, 6, 0) == 0


def test_overlap_day_window_and_empty_window():
    seg = _seg(1, 8, 0, 16, 0)
    assert overlap_minutes(seg, 6, 22, 0) == 480
    assert overlap_minutes(seg, 8, 8, 0) == 0  # leeres Fenster


# ---------------------------------------------------------------------------
# Tages-Kategorisierung
# ---------------------------------------------------------------------------

CFG = SurchargeConfig()  # night 23→6, Sa/So ab 0 Uhr


def test_categorize_weekday_normal():
    # Mi, 1.7.2026 — alles normale Arbeitszeit, keine Nacht.
    res = categorize_day(
        date(2026, 7, 1), [_seg(1, 8, 0, 12, 0)], is_holiday=False, cfg=CFG, tz_offset_min=0
    )
    assert res["NORMALE_ARBEITSZEIT"] == 240
    assert res["SAMSTAG_ARBEIT"] == 0
    assert res["NACHT_ARBEIT"] == 0


def test_categorize_saturday_threshold_13():
    # Sa, 4.7.2026, Zuschlag erst ab 13 Uhr: 10–16 Uhr → 3h normal + 3h Samstag.
    cfg = SurchargeConfig(saturday_from=13)
    res = categorize_day(
        date(2026, 7, 4), [_seg(4, 10, 0, 16, 0)], is_holiday=False, cfg=cfg, tz_offset_min=0
    )
    assert res["NORMALE_ARBEITSZEIT"] == 180
    assert res["SAMSTAG_ARBEIT"] == 180
    assert res["SONNTAG_ARBEIT"] == 0


def test_categorize_sunday_threshold():
    # So, 5.7.2026, Zuschlag ab 10 Uhr: 8–12 Uhr → 2h normal + 2h Sonntag.
    cfg = SurchargeConfig(sunday_from=10)
    res = categorize_day(
        date(2026, 7, 5), [_seg(5, 8, 0, 12, 0)], is_holiday=False, cfg=cfg, tz_offset_min=0
    )
    assert res["NORMALE_ARBEITSZEIT"] == 120
    assert res["SONNTAG_ARBEIT"] == 120


def test_categorize_holiday_beats_sunday():
    # Feiertag auf einem Sonntag → alles FEIERTAG_ARBEIT, kein Sonntags-Split.
    cfg = SurchargeConfig(sunday_from=10)
    res = categorize_day(
        date(2026, 7, 5), [_seg(5, 8, 0, 12, 0)], is_holiday=True, cfg=cfg, tz_offset_min=0
    )
    assert res["FEIERTAG_ARBEIT"] == 240
    assert res["SONNTAG_ARBEIT"] == 0
    assert res["NORMALE_ARBEITSZEIT"] == 0


def test_categorize_night_overlaps_saturday():
    # Sa 22:00–02:00: auf den Sa entfallen 22–24 Uhr = 120 min Samstag,
    # davon 23–24 Uhr = 60 min zusätzlich Nacht (Zuschlag überlappt).
    seg = WorkSegment(start=_dt(4, 22), end=_dt(5, 2))
    res = categorize_day(date(2026, 7, 4), [seg], is_holiday=False, cfg=CFG, tz_offset_min=0)
    assert res["SAMSTAG_ARBEIT"] == 120
    assert res["NACHT_ARBEIT"] == 60
    # Der Rest (00–02 Uhr) gehört zum Aufruf für den Sonntag.
    res_sun = categorize_day(date(2026, 7, 5), [seg], is_holiday=False, cfg=CFG, tz_offset_min=0)
    assert res_sun["SONNTAG_ARBEIT"] == 120
    assert res_sun["NACHT_ARBEIT"] == 120


def test_categorize_with_tz_offset():
    # 20:00–22:00 UTC = 22:00–24:00 lokal (CEST) am Mi → 120 normal, 60 Nacht.
    res = categorize_day(
        date(2026, 7, 1), [_seg(1, 20, 0, 22, 0)], is_holiday=False, cfg=CFG, tz_offset_min=120
    )
    assert res["NORMALE_ARBEITSZEIT"] == 120
    assert res["NACHT_ARBEIT"] == 60


# ---------------------------------------------------------------------------
# Bezahlte Feiertage & Abwesenheit
# ---------------------------------------------------------------------------


def test_paid_holiday_credited_on_weekday():
    # Fr, 3.7.2026 als Feiertag, nicht gearbeitet, nicht abwesend → Tagessoll.
    assert paid_holiday_minutes([date(2026, 7, 3)], set(), set(), 480) == 480


def test_paid_holiday_no_credit_when_worked():
    # Am Feiertag gearbeitet → keine Gutschrift (Arbeit läuft als FEIERTAG_ARBEIT).
    h = date(2026, 7, 3)
    assert paid_holiday_minutes([h], {h}, set(), 480) == 0


def test_paid_holiday_no_credit_when_absent():
    # Urlaub/Krank am Feiertag → keine Doppel-Gutschrift.
    h = date(2026, 7, 3)
    assert paid_holiday_minutes([h], set(), {h}, 480) == 0


def test_paid_holiday_weekend_not_credited():
    # Sa, 4.7.2026 als Feiertag → keine Gutschrift (nur Mo–Fr).
    assert paid_holiday_minutes([date(2026, 7, 4)], set(), set(), 480) == 0
    # Gemischt: nur der Werktags-Feiertag zählt.
    assert paid_holiday_minutes([date(2026, 7, 3), date(2026, 7, 4)], set(), set(), 480) == 480


def test_absence_minutes_half_day():
    assert absence_minutes(1.5, 480) == 720
    assert absence_minutes(0.5, 462) == 231
    assert absence_minutes(0.0, 480) == 0
