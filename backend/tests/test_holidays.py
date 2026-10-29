"""Tests for the German holiday engine."""

from __future__ import annotations

from datetime import date

from zeiterfassung.core.holidays import (
    get_holidays,
    is_holiday,
    is_sunday,
    is_weekend,
    working_days_between,
)


class TestEasterBasedHolidays:
    """Karfreitag and Ostermontag — calculated from Easter Sunday."""

    def test_easter_2024_is_march_31(self) -> None:
        holidays = get_holidays(2024, "HH")
        assert date(2024, 3, 31) in holidays  # Ostersonntag
        assert holidays[date(2024, 3, 31)] == "Ostersonntag"

    def test_ostermontag_2024_is_april_1(self) -> None:
        holidays = get_holidays(2024, "HH")
        assert date(2024, 4, 1) in holidays
        assert holidays[date(2024, 4, 1)] == "Ostermontag"

    def test_karfreitag_2024_is_march_29(self) -> None:
        holidays = get_holidays(2024, "HH")
        assert date(2024, 3, 29) in holidays
        assert holidays[date(2024, 3, 29)] == "Karfreitag"

    def test_pfingstmontag_2025(self) -> None:
        holidays = get_holidays(2025, "HH")
        # Easter 2025 = April 20 → Pfingstmontag = June 9
        assert date(2025, 6, 9) in holidays

    def test_christi_himmelfahrt_2025(self) -> None:
        holidays = get_holidays(2025, "HH")
        # Easter 2025 = April 20 → Himmelfahrt = May 29
        assert date(2025, 5, 29) in holidays


class TestFixedNationalHolidays:
    def test_neujahr(self) -> None:
        assert is_holiday(date(2025, 1, 1), "HH")

    def test_tag_der_arbeit(self) -> None:
        assert is_holiday(date(2025, 5, 1), "HH")

    def test_tag_der_deutschen_einheit(self) -> None:
        assert is_holiday(date(2025, 10, 3), "HH")

    def test_erster_weihnachtstag(self) -> None:
        assert is_holiday(date(2025, 12, 25), "HH")

    def test_zweiter_weihnachtstag(self) -> None:
        assert is_holiday(date(2025, 12, 26), "HH")


class TestRegionalHolidays:
    def test_reformationstag_in_hamburg(self) -> None:
        # HH has Reformationstag since 2018
        assert is_holiday(date(2025, 10, 31), "HH")

    def test_reformationstag_not_in_nw(self) -> None:
        assert not is_holiday(date(2025, 10, 31), "NW")

    def test_heilige_drei_koenige_in_bw(self) -> None:
        assert is_holiday(date(2025, 1, 6), "BW")

    def test_heilige_drei_koenige_not_in_hh(self) -> None:
        assert not is_holiday(date(2025, 1, 6), "HH")

    def test_fronleichnam_in_bw(self) -> None:
        # Fronleichnam 2025: Easter Apr 20 + 60 = June 19
        assert is_holiday(date(2025, 6, 19), "BW")

    def test_fronleichnam_not_in_hh(self) -> None:
        assert not is_holiday(date(2025, 6, 19), "HH")

    def test_allerheiligen_in_nw(self) -> None:
        assert is_holiday(date(2025, 11, 1), "NW")

    def test_allerheiligen_not_in_hh(self) -> None:
        assert not is_holiday(date(2025, 11, 1), "HH")

    def test_buss_und_bettag_in_sn(self) -> None:
        # Sachsen 2025: Wed before Nov 23 = Nov 19
        assert is_holiday(date(2025, 11, 19), "SN")

    def test_buss_und_bettag_not_in_hh(self) -> None:
        assert not is_holiday(date(2025, 11, 19), "HH")

    def test_frauentag_in_berlin_since_2019(self) -> None:
        assert is_holiday(date(2025, 3, 8), "BE")
        assert not is_holiday(date(2018, 3, 8), "BE")

    def test_frauentag_not_in_hamburg(self) -> None:
        assert not is_holiday(date(2025, 3, 8), "HH")


class TestHelpers:
    def test_is_sunday(self) -> None:
        assert is_sunday(date(2025, 1, 5))  # Sunday
        assert not is_sunday(date(2025, 1, 6))  # Monday

    def test_is_weekend(self) -> None:
        assert is_weekend(date(2025, 1, 4))  # Saturday
        assert is_weekend(date(2025, 1, 5))  # Sunday
        assert not is_weekend(date(2025, 1, 6))  # Monday

    def test_working_days_between_full_week(self) -> None:
        # Jan 6-10 2025 is Mon-Fri; no holidays in HH that week
        days = working_days_between(date(2025, 1, 6), date(2025, 1, 10), "HH")
        assert days == 5

    def test_working_days_between_week_with_holiday(self) -> None:
        # Tag der Arbeit 2025 is May 1 (Thursday)
        # Apr 28 Mon – May 2 Fri: 4 working days (May 1 is holiday)
        days = working_days_between(date(2025, 4, 28), date(2025, 5, 2), "HH")
        assert days == 4

    def test_working_days_same_day_not_holiday(self) -> None:
        assert working_days_between(date(2025, 1, 6), date(2025, 1, 6), "HH") == 1

    def test_working_days_same_day_holiday(self) -> None:
        assert working_days_between(date(2025, 1, 1), date(2025, 1, 1), "HH") == 0


class TestCaching:
    def test_repeated_calls_return_same_result(self) -> None:
        h1 = get_holidays(2025, "HH")
        h2 = get_holidays(2025, "HH")
        assert h1 == h2
        assert h1 is h2  # LRU cache returns same dict object
