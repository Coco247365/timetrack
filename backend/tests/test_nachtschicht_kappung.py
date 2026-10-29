"""Mitternachts-Fortsetzung: ein Segmentbeginn um exakt 00:00 Lokalzeit wird
nie durch "fruehester Beginn" oder ein Stempel-Fenster nach vorn geklemmt.

Anlass (Spec 2026-09-15): Der Tages-Editor legt eine Nachtschicht als
Geht 23:59 / Kommt 00:00 / Geht 06:15 an. Bei Beta (Lohnprofil
fruehester_beginn 06:00) wurde das Kommt 00:00 auf 06:00 gekappt — vom
Morgenteil der Schicht blieben 15 min, am Folgetag 0 h.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from zeiterfassung.models.clock import EventType
from zeiterfassung.payroll.rules import (
    WorkSegment,
    clip_segments_earliest_start,
    clip_segments_window_local_tz,
    is_midnight_continuation,
)
from zeiterfassung.policy.worktime import Window
from zeiterfassung.services import clock_service

BERLIN = ZoneInfo("Europe/Berlin")


def test_regelwerk_kappt_mitternachts_start_nicht() -> None:
    # CEST: 00:00 lokal = 22:00 UTC Vortag; Kappung "fruehester Beginn 06:00".
    nacht = WorkSegment(start=datetime(2026, 9, 8, 22, 0), end=datetime(2026, 9, 9, 4, 15))
    frueh = WorkSegment(start=datetime(2026, 9, 9, 3, 19), end=datetime(2026, 9, 9, 12, 0))
    out = clip_segments_earliest_start([nacht, frueh], 6 * 60, 120)
    assert out[0] == nacht  # 00:00–06:15 bleibt komplett
    assert out[1].start == datetime(2026, 9, 9, 4, 0)  # 05:19 -> 06:00 wie bisher
    assert is_midnight_continuation(datetime(2026, 9, 9, 0, 0)) is True
    assert is_midnight_continuation(datetime(2026, 9, 9, 0, 1)) is False


def test_fenster_kappt_mitternachts_start_nicht() -> None:
    nacht = WorkSegment(start=datetime(2026, 9, 8, 22, 0), end=datetime(2026, 9, 9, 4, 15))
    out = clip_segments_window_local_tz([nacht], Window(start_min=6 * 60, end_min=16 * 60), BERLIN)
    assert out == [nacht]


@pytest.mark.asyncio
async def test_tagesbogen_zaehlt_morgenteil_der_nachtschicht(db, tenant, employee) -> None:
    tenant.settings = {
        **(tenant.settings or {}),
        "wage_profiles": [{"id": "beta", "name": "Beta", "fruehester_beginn": "06:00"}],
    }
    employee.wage_profile_id = "beta"
    await db.flush()
    # Kommt 00:00 lokal (22:00 UTC Vortag), Geht 06:15 lokal (04:15 UTC).
    await clock_service.add_manual_event(
        db,
        tenant.id,
        employee.id,
        EventType.PUNCH_IN,
        datetime(2026, 9, 8, 22, 0, tzinfo=UTC),
        None,
        "Nachtschicht",
    )
    await clock_service.add_manual_event(
        db,
        tenant.id,
        employee.id,
        EventType.PUNCH_OUT,
        datetime(2026, 9, 9, 4, 15, tzinfo=UTC),
        None,
        "Nachtschicht",
    )
    ts = await clock_service.recompute_daily_timesheet(db, tenant.id, employee.id, date(2026, 9, 9))
    # 6:15 Anwesenheit, 45 min Betriebspause (ab 28.08.) -> 5:30 — nicht 0:15.
    assert ts.net_work_minutes == 330
    assert ts.is_night_work is True  # echte Nachtarbeit 00:00–06:00

    # Geht VOR der Schwelle (05:45): darf nicht auf 06:00 verlaengert werden —
    # die Beginn-Grenze gilt fuer das Fortsetzungssegment gar nicht. 5:45 - 45 = 5:00.
    await clock_service.add_manual_event(
        db,
        tenant.id,
        employee.id,
        EventType.PUNCH_IN,
        datetime(2026, 9, 10, 22, 0, tzinfo=UTC),
        None,
        "Nachtschicht",  # 00:00 lokal 11.09.
    )
    await clock_service.add_manual_event(
        db,
        tenant.id,
        employee.id,
        EventType.PUNCH_OUT,
        datetime(2026, 9, 11, 3, 45, tzinfo=UTC),
        None,
        "Nachtschicht",  # 05:45 lokal
    )
    ts3 = await clock_service.recompute_daily_timesheet(
        db, tenant.id, employee.id, date(2026, 9, 11)
    )
    assert ts3.net_work_minutes == 300
    # Tagesansicht zeigt dieselbe gewertete Zeit (05:45, nicht 06:00).
    ev = await clock_service.get_day_events(db, tenant.id, employee.id, date(2026, 9, 11))
    assert [e["counted_time"][11:16] for e in ev] == ["22:00", "03:45"]

    # Gegenprobe: ein echter Fruehstempler wird weiter ab 06:00 gewertet.
    await clock_service.add_manual_event(
        db,
        tenant.id,
        employee.id,
        EventType.PUNCH_IN,
        datetime(2026, 9, 10, 3, 19, tzinfo=UTC),
        None,
        "Frueh",  # 05:19 lokal
    )
    await clock_service.add_manual_event(
        db,
        tenant.id,
        employee.id,
        EventType.PUNCH_OUT,
        datetime(2026, 9, 10, 12, 0, tzinfo=UTC),
        None,
        "Frueh",  # 14:00 lokal
    )
    ts2 = await clock_service.recompute_daily_timesheet(
        db, tenant.id, employee.id, date(2026, 9, 10)
    )
    assert ts2.net_work_minutes == 8 * 60 - 45  # 06:00–14:00 minus 45 min
