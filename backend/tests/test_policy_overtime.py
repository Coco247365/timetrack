"""Reine Policy-Tests (ohne DB): Überstunden-Tagesregel + Kommt/Geht-Reihenfolge."""

from datetime import UTC, date, datetime

from zeiterfassung.policy import timekeeping
from zeiterfassung.policy.overtime import (
    DayFacts,
    classify_day,
    daily_target_minutes,
    weekly_target_minutes,
)

D = date(2026, 7, 1)  # Mittwoch


def _facts(**kw) -> DayFacts:
    base = dict(
        day=D,
        weekend=False,
        holiday=False,
        coverage=0.0,
        net_minutes=0,
        has_timesheet=False,
        worked=False,
        is_auto=False,
        is_past=True,
        is_today_or_past=True,
        within_cutoff=True,
    )
    base.update(kw)
    return DayFacts(**base)


def test_daily_target_minutes():
    assert daily_target_minutes(40) == 480
    assert daily_target_minutes(30) == 360
    assert daily_target_minutes(0) == 480  # Fallback
    assert daily_target_minutes(None) == 480


# Mo–Do 8:15 (495), Fr 5:30 (330), Sa/So 0 — wie im Alt-System-Screenshot.
PLAN = [495, 495, 495, 495, 330, 0, 0]


def test_daily_target_per_weekday_plan():
    # weekday: 0=Mo .. 6=So
    assert daily_target_minutes(40, weekday=0, weekday_minutes=PLAN) == 495  # Mo 8:15
    assert daily_target_minutes(40, weekday=4, weekday_minutes=PLAN) == 330  # Fr 5:30
    assert daily_target_minutes(40, weekday=5, weekday_minutes=PLAN) == 0  # Sa
    # Ohne Plan → Fallback Wochenstunden÷5, unabhängig vom Wochentag.
    assert daily_target_minutes(40, weekday=4, weekday_minutes=None) == 480
    # Ohne weekday → Fallback, auch wenn Plan da ist.
    assert daily_target_minutes(40, weekday_minutes=PLAN) == 480


def test_weekly_target_minutes():
    assert weekly_target_minutes(40) == 2400  # 40 h
    assert weekly_target_minutes(40, PLAN) == 4 * 495 + 330  # 38:30 = 2310
    assert weekly_target_minutes(None, None) == 2400


def test_friday_short_day_overtime_uses_weekday_soll():
    """Fr 8:44 gearbeitet gegen Soll 5:30 → +3:14 (Screenshot 12.Fr)."""
    r = classify_day(
        _facts(worked=True, has_timesheet=True, net_minutes=524, day=date(2026, 7, 3)), 330
    )
    assert r.delta_minutes == 194  # 3 h 14 min


def test_worked_over_and_under_soll():
    over = classify_day(_facts(worked=True, has_timesheet=True, net_minutes=510), 480)
    assert over.delta_minutes == 30 and over.absent is False
    under = classify_day(_facts(worked=True, has_timesheet=True, net_minutes=360), 480)
    assert under.delta_minutes == -120


def test_weekend_worked_does_not_touch_balance():
    """WE-Arbeit hat eigene Lohnart (Zuschläge) — kein Überstunden-Plus (#21)."""
    r = classify_day(_facts(worked=True, has_timesheet=True, net_minutes=390, weekend=True), 480)
    assert r.target_minutes == 0 and r.delta_minutes == 0
    assert r.net_minutes == 390  # Ist bleibt sichtbar


def test_holiday_worked_does_not_touch_balance():
    r = classify_day(_facts(worked=True, has_timesheet=True, net_minutes=480, holiday=True), 480)
    assert r.delta_minutes == 0


def test_weekend_worked_counts_when_profile_flag_set():
    """Profil-Regel 'wochenende_ins_konto': WE-Arbeit zaehlt normal ins Konto
    (Gegenteil von #21 — z. B. Beta: Samstagsarbeit -> Gleitzeitkonto)."""
    r = classify_day(
        _facts(worked=True, has_timesheet=True, net_minutes=295, weekend=True),
        480,
        weekend_counts=True,
    )
    assert r.target_minutes == 0 and r.delta_minutes == 295
    assert r.net_minutes == 295


def test_holiday_worked_counts_when_profile_flag_set():
    r = classify_day(
        _facts(worked=True, has_timesheet=True, net_minutes=300, holiday=True),
        480,
        weekend_counts=True,
    )
    assert r.delta_minutes == 300


def test_weekend_counts_default_is_false_existing_signature_unchanged():
    """Default (kein Parameter uebergeben) bleibt exakt das bisherige Verhalten (#21)."""
    r = classify_day(_facts(worked=True, has_timesheet=True, net_minutes=390, weekend=True), 480)
    assert r.target_minutes == 0 and r.delta_minutes == 0


def test_worked_on_vacation_day_counts_normal():
    """Arbeit trotz Urlaub: Tag zählt normal (kein Gratis-Plus), Urlaub wird
    über die Urlaubsbilanz zurückgegeben (#22)."""
    r = classify_day(
        _facts(
            worked=True, has_timesheet=True, net_minutes=480, coverage=1.0, vacation_coverage=1.0
        ),
        480,
    )
    assert r.target_minutes == 480 and r.delta_minutes == 0


def test_worked_half_sick_day_keeps_coverage():
    """Halbtags krank + gearbeitet: Krank deckt weiter seinen Anteil (#23) —
    4 h Arbeit gegen halbes Soll = ausgeglichen."""
    r = classify_day(
        _facts(
            worked=True, has_timesheet=True, net_minutes=240, coverage=0.5, vacation_coverage=0.0
        ),
        480,
    )
    assert r.target_minutes == 240 and r.delta_minutes == 0


def test_full_absence_no_work_does_not_count():
    assert classify_day(_facts(coverage=1.0, has_timesheet=True), 480) is None


def test_half_absence_no_work_is_minus_not_unexcused():
    r = classify_day(_facts(coverage=0.5, has_timesheet=True), 480)
    assert r.delta_minutes == -240 and r.absent is False


def test_stamper_gap_is_unexcused():
    r = classify_day(_facts(is_auto=False, has_timesheet=False, is_past=True), 480)
    assert r.delta_minutes == -480 and r.absent is True


def test_auto_gap_without_timesheet_does_not_count():
    assert classify_day(_facts(is_auto=True, has_timesheet=False), 480) is None


def test_explicit_unworked_row_is_unexcused_any_mode():
    r = classify_day(_facts(is_auto=True, has_timesheet=True, is_today_or_past=True), 480)
    assert r.delta_minutes == -480 and r.absent is True


def test_before_cutoff_counts_nothing():
    assert (
        classify_day(
            _facts(worked=True, has_timesheet=True, net_minutes=600, within_cutoff=False), 480
        )
        is None
    )


def test_after_termination_counts_nothing():
    """Nach dem Austritt (within_cutoff=False) kein Minus mehr für Fehltage (#25)."""
    # Leerer Werktag nach Austritt: würde sonst −Soll geben, jetzt None.
    assert (
        classify_day(_facts(has_timesheet=True, is_today_or_past=True, within_cutoff=False), 480)
        is None
    )


def _t(h):
    return datetime(2026, 7, 1, h, 0, tzinfo=UTC)


def test_punch_order_ok_and_bad():
    assert timekeeping.check_punch_order([(True, _t(8)), (False, _t(17))]) == []
    assert (
        timekeeping.check_punch_order([(False, _t(7)), (True, _t(8))])[0].code
        == "punch.out_before_in"
    )
    assert (
        timekeeping.check_punch_order([(True, _t(8)), (False, _t(8))])[0].code
        == "punch.out_before_in"
    )
    assert (
        timekeeping.check_punch_order([(True, _t(8)), (True, _t(9))])[0].code == "punch.double_in"
    )
