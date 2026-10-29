"""Tests for ArbZG working time law rules."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from zeiterfassung.core.arbzg import (
    check_break_compliance,
    check_daily_hours,
    check_rest_period,
    required_break_minutes,
    validate_timesheet,
)


class TestDailyHours:
    """check_daily_hours nimmt die ECHTE Netto-Arbeitszeit. Grace = 15 min."""

    def test_8h_net_is_compliant(self) -> None:
        # 8h net → exactly at limit, no violation
        violations = check_daily_hours(480)
        assert violations == []

    def test_8h15_net_no_info_disabled(self) -> None:
        # 8h15m net: die "liegt leicht über 8 h"-Info ist auf Wunsch deaktiviert.
        assert check_daily_hours(495) == []

    def test_8h30_net_no_warning_disabled(self) -> None:
        # 8h30m net: die 8h-Überschreitungs-Warnung ist auf Wunsch deaktiviert
        # → keine Meldung (nur der harte >10h-Fehler bleibt).
        assert check_daily_hours(510) == []

    def test_10h_net_is_warning_within_grace(self) -> None:
        # 10h10m net (within 15min grace of 10h) → warning not error
        violations = check_daily_hours(610)
        assert len(violations) == 1
        assert violations[0].severity == "warning"

    def test_10h30_net_triggers_error(self) -> None:
        # 10h30m net (beyond grace) → error
        violations = check_daily_hours(630)
        assert len(violations) == 1
        assert violations[0].severity == "error"

    def test_5h_is_fine(self) -> None:
        assert check_daily_hours(300) == []

    def test_net_8h50_no_warning(self) -> None:
        # Realfall aus Screenshot: Brutto 9:18, Pause 0:28 → Netto 8:50.
        # 8h50m liegt im 8–10h-Bereich → Warnung deaktiviert, keine Meldung.
        assert check_daily_hours(530) == []


class TestBreakCompliance:
    def test_6h_work_no_break_required(self) -> None:
        # Exactly 6 h → no break required
        violations = check_break_compliance(360, 0)
        assert violations == []

    def test_6h_1min_work_needs_30min_break(self) -> None:
        violations = check_break_compliance(361, 0)
        assert len(violations) == 1
        assert violations[0].severity == "error"
        assert "30 Min." in violations[0].message

    def test_6h_1min_work_with_30min_break_ok(self) -> None:
        violations = check_break_compliance(361, 30)
        assert violations == []

    def test_borderline_9h_presence_with_30min_break_is_ok(self) -> None:
        # 9:01 Anwesenheit mit 30 min Pause = 8:31 ARBEITSZEIT ≤ 9 h → konform.
        # (§4 bemisst sich an der Arbeitszeit ohne Pausen, nicht an der Spanne.)
        violations = check_break_compliance(541, 30)
        assert violations == []

    def test_9h_net_work_needs_45min_break(self) -> None:
        # 9:46 Anwesenheit mit 30 min Pause = 9:16 Arbeitszeit > 9 h → 45 nötig.
        violations = check_break_compliance(586, 30)
        assert len(violations) == 1
        assert "45 Min." in violations[0].message

    def test_9h_work_with_45min_break_ok(self) -> None:
        violations = check_break_compliance(541, 45)
        assert violations == []

    def test_9h_work_with_60min_break_ok(self) -> None:
        violations = check_break_compliance(541, 60)
        assert violations == []


class TestRestPeriod:
    def _dt(self, hour: int) -> datetime:
        return datetime(2025, 1, 7, hour, 0, tzinfo=UTC)

    def test_11h_rest_is_exactly_compliant(self) -> None:
        prev_end = self._dt(18)  # 18:00
        next_start = self._dt(5)  # 05:00 next day → 11 h rest
        from datetime import timedelta

        next_start = prev_end + timedelta(hours=11)
        violations = check_rest_period(prev_end, next_start)
        assert violations == []

    def test_10h_rest_is_violation(self) -> None:
        from datetime import timedelta

        prev_end = self._dt(18)
        next_start = prev_end + timedelta(hours=10)
        violations = check_rest_period(prev_end, next_start)
        assert len(violations) == 1
        assert violations[0].severity == "error"
        assert "ArbZG §5" in violations[0].rule

    def test_12h_rest_is_fine(self) -> None:
        from datetime import timedelta

        prev_end = self._dt(18)
        next_start = prev_end + timedelta(hours=12)
        violations = check_rest_period(prev_end, next_start)
        assert violations == []


class TestValidateTimesheet:
    def test_normal_8h_day_is_clean(self) -> None:
        # 8h gross, 30min break → 7h30m net → no violations at all
        result = validate_timesheet(gross_work_minutes=480, break_minutes=30)
        assert result.is_compliant
        assert not result.has_warnings

    def test_long_day_without_break_has_violations(self) -> None:
        # 9h gross, 0 break → 9h net → §3 warning + §4 break violation
        result = validate_timesheet(gross_work_minutes=540, break_minutes=0)
        assert result.has_warnings

    def test_excessive_hours_has_error(self) -> None:
        # 11h40m gross, 60min break → 10h40m net → error (beyond 10h+grace)
        result = validate_timesheet(gross_work_minutes=700, break_minutes=60)
        assert not result.is_compliant

    def test_split_shift_uses_real_net_work_time(self) -> None:
        # Geteilte Schicht (z. B. Nachtschicht-Split 00:00–04:45 + 19:30–23:59):
        # Tagesspanne ~24 h, echte Arbeitszeit aber nur ~9 h. Die Lücke zwischen
        # den Sessions ist weder Arbeit noch Pause — mit explizit übergebener
        # Netto-Arbeitszeit darf §3 nicht anschlagen.
        result = validate_timesheet(
            gross_work_minutes=1439,
            break_minutes=45,
            net_work_minutes=509,
        )
        assert not any(v.rule == "ArbZG §3" for v in result.violations)

    def test_split_shift_without_net_would_flag(self) -> None:
        # Kontrolltest: OHNE echte Netto-Arbeitszeit (Fallback brutto − Pausen)
        # ergäbe dieselbe Spanne 23,2 h → §3-Fehler. Belegt, dass der Parameter
        # den Unterschied macht.
        result = validate_timesheet(gross_work_minutes=1439, break_minutes=45)
        assert any(v.rule == "ArbZG §3" and v.severity == "error" for v in result.violations)

    def test_rest_period_check_included_when_prev_end_provided(self) -> None:
        from datetime import datetime

        prev_end = datetime(2025, 1, 7, 22, 0, tzinfo=UTC)
        next_start = datetime(2025, 1, 8, 5, 0, tzinfo=UTC)  # only 7 h rest
        result = validate_timesheet(
            gross_work_minutes=480,
            break_minutes=30,
            start_time=next_start,
            prev_day_end_time=prev_end,
        )
        assert any("§5" in v.rule for v in result.violations)


class TestRequiredBreakMinutes:
    @pytest.mark.parametrize(
        "minutes, expected",
        [
            (300, 0),  # 5 h — no break required
            (360, 0),  # exactly 6 h — boundary: no break yet
            (361, 30),  # just over 6 h
            (540, 30),  # exactly 9 h — still 30 min tier
            (541, 45),  # just over 9 h
            (600, 45),  # 10 h
        ],
    )
    def test_thresholds(self, minutes: int, expected: int) -> None:
        assert required_break_minutes(minutes) == expected
