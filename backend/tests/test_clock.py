"""Tests for clock service: punch recording, timesheet recomputation, anomaly detection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from zeiterfassung.models.clock import EventSource, EventType, PunchEvent, TimesheetStatus
from zeiterfassung.services.clock_service import (
    UNDO_GRACE_SECONDS,
    InvalidPunchStateError,
    NothingToUndoError,
    PunchRateLimitError,
    detect_anomalies,
    get_current_clock_status,
    recompute_daily_timesheet,
    record_punch,
    undo_last_punch,
)


def _dt(year: int, month: int, day: int, hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


class TestRecordPunch:
    async def test_record_punch_in_creates_event(self, db, tenant, employee) -> None:
        event = await record_punch(
            db=db,
            tenant_id=tenant.id,
            employee_id=employee.id,
            event_type=EventType.PUNCH_IN,
            source=EventSource.WEB_PORTAL,
            event_time=_dt(2025, 3, 10, 8, 0),
        )
        assert event.id is not None
        assert event.event_type == EventType.PUNCH_IN
        assert event.employee_id == employee.id
        assert event.tenant_id == tenant.id

    async def test_punch_time_is_floored_to_full_minute(self, db, tenant, employee) -> None:
        """Stempelzeiten zählen immer ab der vollen Minute: 11:17:34 → 11:17:00.

        Sekunden dürfen nie in die Zeitrechnung einfließen — weder beim
        Live-Stempeln (Terminal/Web) noch bei manuell nachgetragenen Events."""
        event = await record_punch(
            db=db,
            tenant_id=tenant.id,
            employee_id=employee.id,
            event_type=EventType.PUNCH_IN,
            source=EventSource.NFC_TERMINAL,
            event_time=_dt(2025, 3, 10, 11, 17, 34),
        )
        assert event.event_time.second == 0 and event.event_time.microsecond == 0
        assert event.event_time.minute == 17, "abrunden, nicht kaufmännisch runden"

        from zeiterfassung.services.clock_service import add_manual_event

        manual = await add_manual_event(
            db,
            tenant.id,
            employee.id,
            EventType.PUNCH_OUT,
            _dt(2025, 3, 10, 19, 23, 58),
            None,
            "Test",
        )
        assert manual.event_time.second == 0 and manual.event_time.microsecond == 0
        assert manual.event_time.minute == 23, "19:23:58 zählt als 19:23:00"

    async def test_record_punch_out_creates_event(self, db, tenant, employee) -> None:
        await record_punch(
            db=db,
            tenant_id=tenant.id,
            employee_id=employee.id,
            event_type=EventType.PUNCH_IN,
            source=EventSource.WEB_PORTAL,
            event_time=_dt(2025, 3, 10, 8, 0),
        )
        out = await record_punch(
            db=db,
            tenant_id=tenant.id,
            employee_id=employee.id,
            event_type=EventType.PUNCH_OUT,
            source=EventSource.WEB_PORTAL,
            event_time=_dt(2025, 3, 10, 17, 0),
        )
        assert out.event_type == EventType.PUNCH_OUT

    async def test_punch_triggers_timesheet_computation(self, db, tenant, employee) -> None:
        await record_punch(
            db=db,
            tenant_id=tenant.id,
            employee_id=employee.id,
            event_type=EventType.PUNCH_IN,
            source=EventSource.WEB_PORTAL,
            event_time=_dt(2025, 3, 10, 8, 0),
        )
        await record_punch(
            db=db,
            tenant_id=tenant.id,
            employee_id=employee.id,
            event_type=EventType.PUNCH_OUT,
            source=EventSource.WEB_PORTAL,
            event_time=_dt(2025, 3, 10, 16, 30),
        )
        # Timesheet should now exist with COMPLETE status
        from datetime import date

        from sqlalchemy import select

        from zeiterfassung.models.clock import DailyTimesheet

        result = await db.execute(
            select(DailyTimesheet).where(
                DailyTimesheet.employee_id == employee.id,
                DailyTimesheet.work_date == date(2025, 3, 10),
            )
        )
        ts = result.scalar_one_or_none()
        assert ts is not None
        assert ts.gross_work_minutes == 510  # 8.5 h
        # 8,5 h ohne Pausenstempel: gesetzliche Mindestpause (30 min) wird
        # automatisch angesetzt → kein ArbZG-§4-Verstoß, Tag ist COMPLETE.
        assert ts.break_minutes == 30
        assert ts.net_work_minutes == 480
        assert ts.status == TimesheetStatus.COMPLETE


class TestTimesheetRecomputation:
    async def test_recompute_8h_shift(self, db, tenant, employee) -> None:
        from datetime import date

        await record_punch(
            db=db,
            tenant_id=tenant.id,
            employee_id=employee.id,
            event_type=EventType.PUNCH_IN,
            source=EventSource.WEB_PORTAL,
            event_time=_dt(2025, 3, 11, 8, 0),
        )
        await record_punch(
            db=db,
            tenant_id=tenant.id,
            employee_id=employee.id,
            event_type=EventType.PUNCH_OUT,
            source=EventSource.WEB_PORTAL,
            event_time=_dt(2025, 3, 11, 16, 30),
        )
        ts = await recompute_daily_timesheet(db, tenant.id, employee.id, date(2025, 3, 11))
        assert ts.gross_work_minutes == 510
        # Keine Pause gestempelt → automatische Pflichtpause 30 min (ArbZG §4).
        assert ts.break_minutes == 30
        assert ts.net_work_minutes == 480

    async def test_recompute_with_break(self, db, tenant, employee) -> None:
        from datetime import date

        events = [
            (EventType.PUNCH_IN, _dt(2025, 3, 12, 8, 0)),
            (EventType.BREAK_START, _dt(2025, 3, 12, 12, 0)),
            (EventType.BREAK_END, _dt(2025, 3, 12, 12, 30)),
            (EventType.PUNCH_OUT, _dt(2025, 3, 12, 17, 0)),
        ]
        for etype, etime in events:
            await record_punch(
                db=db,
                tenant_id=tenant.id,
                employee_id=employee.id,
                event_type=etype,
                source=EventSource.WEB_PORTAL,
                event_time=etime,
            )

        ts = await recompute_daily_timesheet(db, tenant.id, employee.id, date(2025, 3, 12))
        assert ts.gross_work_minutes == 540  # 9 h raw
        assert ts.break_minutes == 30
        assert ts.net_work_minutes == 510

    async def test_recompute_overtime(self, db, tenant, employee) -> None:
        from datetime import date

        # Employee has 40h/week = 8h/day contracted
        await record_punch(
            db=db,
            tenant_id=tenant.id,
            employee_id=employee.id,
            event_type=EventType.PUNCH_IN,
            source=EventSource.WEB_PORTAL,
            event_time=_dt(2025, 3, 13, 8, 0),
        )
        await record_punch(
            db=db,
            tenant_id=tenant.id,
            employee_id=employee.id,
            event_type=EventType.PUNCH_OUT,
            source=EventSource.WEB_PORTAL,
            event_time=_dt(2025, 3, 13, 19, 0),
        )
        ts = await recompute_daily_timesheet(db, tenant.id, employee.id, date(2025, 3, 13))
        # 11 h Anwesenheit ohne Pausenstempel → 45 min Pflichtpause (ArbZG §4, >9 h):
        # netto 10:15 − 8 h Soll = 135 min Überstunden.
        assert ts.break_minutes == 45
        assert ts.overtime_minutes == 135

    async def test_split_shift_gap_is_not_work_time(self, db, tenant, employee) -> None:
        from datetime import date
        from zoneinfo import ZoneInfo

        berlin = ZoneInfo("Europe/Berlin")

        def _dt_local(hour: int, minute: int) -> datetime:
            # Berliner Ortszeit angeben, aber als UTC-Instant speichern (wie
            # überall im Code/Storage) — nur die Bucketing-/Ableitungslogik
            # rechnet UTC -> Tenant-Lokalzeit, nicht die Event-Erzeugung.
            # 0:00/23:59 Ortszeit sollen auf demselben Berliner Kalendertag
            # bleiben statt an der UTC-Grenze zerschnitten zu werden (#3).
            return datetime(2025, 3, 18, hour, minute, tzinfo=berlin).astimezone(UTC)

        # Nachtschicht-Split: zwei Sessions 00:00–04:45 und 19:30–23:59. Die
        # Lücke dazwischen (Geht → wieder Kommt) ist weder Arbeit noch Pause.
        # §3 bemisst sich an der echten Arbeitszeit (~9 h), nicht an der
        # Tagesspanne (~24 h) → kein "Netto-Arbeitszeit 23,x h"-Flag.
        events = [
            (EventType.PUNCH_IN, _dt_local(0, 0)),
            (EventType.PUNCH_OUT, _dt_local(4, 45)),
            (EventType.PUNCH_IN, _dt_local(19, 30)),
            (EventType.PUNCH_OUT, _dt_local(23, 59)),
        ]
        for etype, etime in events:
            await record_punch(
                db=db,
                tenant_id=tenant.id,
                employee_id=employee.id,
                event_type=etype,
                source=EventSource.WEB_PORTAL,
                event_time=etime,
            )

        ts = await recompute_daily_timesheet(db, tenant.id, employee.id, date(2025, 3, 18))
        assert ts.gross_work_minutes == 1439  # Spanne 00:00–23:59
        # 285 + 269 = 554 min echte Arbeit = Anwesenheit (keine Pausenstempel);
        # Anwesenheit > 6 h → Auto-Pflichtpause 30 min (§4, anwesenheitsbasiert)
        # → netto 524 min. Kein §3-Flag: 8,7 h Arbeitszeit, nicht 23,x h.
        assert ts.break_minutes == 30
        assert ts.net_work_minutes == 524
        assert "§3" not in (ts.flagged_reason or "")
        assert ts.status == TimesheetStatus.COMPLETE

    async def test_holiday_work_flagged(self, db, tenant, employee) -> None:
        from datetime import date

        # May 1 2025 is Tag der Arbeit (holiday)
        await record_punch(
            db=db,
            tenant_id=tenant.id,
            employee_id=employee.id,
            event_type=EventType.PUNCH_IN,
            source=EventSource.WEB_PORTAL,
            event_time=_dt(2025, 5, 1, 9, 0),
        )
        await record_punch(
            db=db,
            tenant_id=tenant.id,
            employee_id=employee.id,
            event_type=EventType.PUNCH_OUT,
            source=EventSource.WEB_PORTAL,
            event_time=_dt(2025, 5, 1, 17, 0),
        )
        ts = await recompute_daily_timesheet(db, tenant.id, employee.id, date(2025, 5, 1))
        assert ts.is_holiday_work is True


class TestTenantLocalDateBucketing:
    """#3: Tage/Stunden werden nach Tenant-Lokalzeit (Europe/Berlin)
    gebuckett, nicht nach UTC-Kalendertag/-Stunde. Die ``tenant``-Fixture hat
    ``timezone="Europe/Berlin"``; alle Zeiten hier sind Winterzeit (CET, UTC+1)."""

    async def test_punch_shortly_after_local_midnight_buckets_to_local_day(
        self, db, tenant, employee
    ) -> None:
        from datetime import date
        from zoneinfo import ZoneInfo

        from sqlalchemy import select

        from zeiterfassung.models.clock import DailyTimesheet

        berlin = ZoneInfo("Europe/Berlin")
        # 00:20 CET am 15.1.2025 == 23:20 UTC am 14.1.2025. Nach UTC-Datum
        # würde der Stempel fälschlich auf den 14. gebuchen (#3).
        local_time = datetime(2025, 1, 15, 0, 20, tzinfo=berlin)
        event_time_utc = local_time.astimezone(UTC)
        assert event_time_utc == datetime(2025, 1, 14, 23, 20, tzinfo=UTC)

        await record_punch(
            db=db,
            tenant_id=tenant.id,
            employee_id=employee.id,
            event_type=EventType.PUNCH_IN,
            source=EventSource.NFC_TERMINAL,
            event_time=event_time_utc,
        )

        async def _timesheet_for(work_date: date) -> DailyTimesheet | None:
            return (
                await db.execute(
                    select(DailyTimesheet).where(
                        DailyTimesheet.tenant_id == tenant.id,
                        DailyTimesheet.employee_id == employee.id,
                        DailyTimesheet.work_date == work_date,
                    )
                )
            ).scalar_one_or_none()

        assert await _timesheet_for(date(2025, 1, 15)) is not None, (
            "Stempel muss auf den Berliner Kalendertag (15.1.) gebucht werden"
        )
        assert await _timesheet_for(date(2025, 1, 14)) is None, (
            "Stempel darf NICHT auf den UTC-Kalendertag (14.1.) gebucht werden"
        )

    async def test_sunday_flag_uses_local_date_not_utc_date(self, db, tenant, employee) -> None:
        from datetime import date
        from zoneinfo import ZoneInfo

        berlin = ZoneInfo("Europe/Berlin")
        # 00:20 CET am Sonntag 19.1.2025 == 23:20 UTC am Samstag 18.1.2025 —
        # nach UTC-Datum wäre das noch Samstag (is_sunday_work fälschlich False).
        local_time = datetime(2025, 1, 19, 0, 20, tzinfo=berlin)
        assert local_time.astimezone(UTC).date() == date(2025, 1, 18)

        await record_punch(
            db=db,
            tenant_id=tenant.id,
            employee_id=employee.id,
            event_type=EventType.PUNCH_IN,
            source=EventSource.NFC_TERMINAL,
            event_time=local_time.astimezone(UTC),
        )
        ts = await recompute_daily_timesheet(db, tenant.id, employee.id, date(2025, 1, 19))
        assert ts.is_sunday_work is True

    async def test_holiday_flag_uses_local_date_not_utc_date(self, db, tenant, employee) -> None:
        from datetime import date
        from zoneinfo import ZoneInfo

        berlin = ZoneInfo("Europe/Berlin")
        # 00:20 CET am Neujahrstag (Feiertag) == 23:20 UTC am 31.12. (kein
        # Feiertag) — nach UTC-Datum würde is_holiday_work fälschlich False sein.
        local_time = datetime(2025, 1, 1, 0, 20, tzinfo=berlin)
        assert local_time.astimezone(UTC).date().day == 31

        await record_punch(
            db=db,
            tenant_id=tenant.id,
            employee_id=employee.id,
            event_type=EventType.PUNCH_IN,
            source=EventSource.NFC_TERMINAL,
            event_time=local_time.astimezone(UTC),
        )
        ts = await recompute_daily_timesheet(db, tenant.id, employee.id, date(2025, 1, 1))
        assert ts.is_holiday_work is True

    async def test_night_window_uses_local_hour_not_utc_hour(self, db, tenant, employee) -> None:
        from datetime import date
        from zoneinfo import ZoneInfo

        berlin = ZoneInfo("Europe/Berlin")
        # 23:15 Ortszeit (Winterzeit CET) == 22:15 UTC. Das Standard-Nachtfenster
        # ist 23-6 Uhr LOKAL; nach UTC-Stunde (22) würde dieser Stempel das
        # Fenster fälschlich verpassen (#3).
        local_time = datetime(2025, 1, 15, 23, 15, tzinfo=berlin)
        event_time_utc = local_time.astimezone(UTC)
        assert event_time_utc.hour == 22  # Beleg: UTC-Stunde allein läge außerhalb 23-6

        await record_punch(
            db=db,
            tenant_id=tenant.id,
            employee_id=employee.id,
            event_type=EventType.PUNCH_IN,
            source=EventSource.NFC_TERMINAL,
            event_time=event_time_utc,
        )
        ts = await recompute_daily_timesheet(db, tenant.id, employee.id, date(2025, 1, 15))
        assert ts.is_night_work is True


class TestClockStatus:
    async def test_initial_status_is_clocked_out(self, db, tenant, employee) -> None:
        result = await get_current_clock_status(db, tenant.id, employee.id)
        assert result["is_clocked_in"] is False
        assert result["is_on_break"] is False
        assert result["last_event"] is None

    async def test_after_punch_in_is_clocked_in(self, db, tenant, employee) -> None:
        await record_punch(
            db=db,
            tenant_id=tenant.id,
            employee_id=employee.id,
            event_type=EventType.PUNCH_IN,
            source=EventSource.WEB_PORTAL,
            event_time=_dt(2025, 3, 14, 8, 0),
        )
        result = await get_current_clock_status(db, tenant.id, employee.id)
        assert result["is_clocked_in"] is True
        assert result["is_on_break"] is False
        assert result["last_event"] is not None
        assert result["clocked_in_since"] is not None

    async def test_after_punch_out_is_clocked_out(self, db, tenant, employee) -> None:
        await record_punch(
            db=db,
            tenant_id=tenant.id,
            employee_id=employee.id,
            event_type=EventType.PUNCH_IN,
            source=EventSource.WEB_PORTAL,
            event_time=_dt(2025, 3, 14, 8, 0),
        )
        await record_punch(
            db=db,
            tenant_id=tenant.id,
            employee_id=employee.id,
            event_type=EventType.PUNCH_OUT,
            source=EventSource.WEB_PORTAL,
            event_time=_dt(2025, 3, 14, 17, 0),
        )
        result = await get_current_clock_status(db, tenant.id, employee.id)
        assert result["is_clocked_in"] is False
        assert result["is_on_break"] is False

    async def test_break_start_shows_on_break(self, db, tenant, employee) -> None:
        await record_punch(
            db=db,
            tenant_id=tenant.id,
            employee_id=employee.id,
            event_type=EventType.PUNCH_IN,
            source=EventSource.WEB_PORTAL,
            event_time=_dt(2025, 3, 14, 8, 0),
        )
        await record_punch(
            db=db,
            tenant_id=tenant.id,
            employee_id=employee.id,
            event_type=EventType.BREAK_START,
            source=EventSource.WEB_PORTAL,
            event_time=_dt(2025, 3, 14, 12, 0),
        )
        result = await get_current_clock_status(db, tenant.id, employee.id)
        assert result["is_clocked_in"] is True
        assert result["is_on_break"] is True
        assert result["break_started_at"] is not None

    async def test_break_end_resumes_work(self, db, tenant, employee) -> None:
        for etype, etime in [
            (EventType.PUNCH_IN, _dt(2025, 3, 14, 8, 0)),
            (EventType.BREAK_START, _dt(2025, 3, 14, 12, 0)),
            (EventType.BREAK_END, _dt(2025, 3, 14, 12, 30)),
        ]:
            await record_punch(
                db=db,
                tenant_id=tenant.id,
                employee_id=employee.id,
                event_type=etype,
                source=EventSource.WEB_PORTAL,
                event_time=etime,
            )
        result = await get_current_clock_status(db, tenant.id, employee.id)
        assert result["is_clocked_in"] is True
        assert result["is_on_break"] is False

    async def test_orphaned_break_after_backfilled_out_is_clocked_out(
        self, db, tenant, employee
    ) -> None:
        # Realfall (20./21.07.2026): Kommen am Vortag, Gehen vergessen.
        # Am Folgetag Terminal-Tap → System sieht offene Schicht → bucht Pause.
        # DANACH trägt der Admin das Gehen für den Vortag nach. Das letzte
        # gültige Event ist nun ein verwaister Pause-Beginn NACH dem Gehen —
        # der Mitarbeiter ist NICHT anwesend, "seit Vortag" wäre falsch.
        from zeiterfassung.services.clock_service import add_manual_event

        await record_punch(
            db=db,
            tenant_id=tenant.id,
            employee_id=employee.id,
            event_type=EventType.PUNCH_IN,
            source=EventSource.NFC_TERMINAL,
            event_time=_dt(2025, 3, 13, 11, 8),
        )
        await record_punch(
            db=db,
            tenant_id=tenant.id,
            employee_id=employee.id,
            event_type=EventType.BREAK_START,
            source=EventSource.NFC_TERMINAL,
            event_time=_dt(2025, 3, 14, 7, 5),
        )
        await add_manual_event(
            db,
            tenant.id,
            employee.id,
            EventType.PUNCH_OUT,
            _dt(2025, 3, 13, 15, 0),
            None,
            "Gehen nachgetragen",
        )

        result = await get_current_clock_status(db, tenant.id, employee.id)
        assert result["is_clocked_in"] is False
        assert result["is_on_break"] is False
        assert result["clocked_in_since"] is None


class TestAnomalyDetection:
    def _make_ts(self, **kwargs):
        """Create a lightweight stand-in for DailyTimesheet using SimpleNamespace."""
        from datetime import date
        from types import SimpleNamespace

        from zeiterfassung.models.clock import TimesheetStatus

        defaults = dict(
            id=1,
            tenant_id=None,
            employee_id=None,
            work_date=date(2025, 3, 14),
            gross_work_minutes=480,
            net_work_minutes=480,
            break_minutes=30,
            overtime_minutes=0,
            status=TimesheetStatus.COMPLETE,
            is_night_work=False,
            is_sunday_work=False,
            is_holiday_work=False,
            start_time=None,
            end_time=None,
            computed_from_events=True,
        )
        defaults.update(kwargs)
        return SimpleNamespace(**defaults)

    def test_no_anomalies_for_normal_day(self) -> None:
        ts = self._make_ts()
        assert detect_anomalies(ts) == []

    def test_long_work_time_flagged(self) -> None:
        ts = self._make_ts(gross_work_minutes=620)
        anomalies = detect_anomalies(ts)
        assert any("10-hour" in a for a in anomalies)

    def test_missing_break_after_6h_flagged(self) -> None:
        ts = self._make_ts(gross_work_minutes=400, break_minutes=0)
        anomalies = detect_anomalies(ts)
        assert any("break" in a.lower() for a in anomalies)

    def test_clocked_out_without_in_flagged(self) -> None:
        ts = self._make_ts(
            start_time=None,
            end_time=_dt(2025, 3, 14, 17, 0),
        )
        anomalies = detect_anomalies(ts)
        assert any("without clock-in" in a for a in anomalies)

    def test_clocked_in_without_out_flagged(self) -> None:
        ts = self._make_ts(
            start_time=_dt(2025, 3, 14, 8, 0),
            end_time=None,
        )
        anomalies = detect_anomalies(ts)
        assert any("no clock-out" in a for a in anomalies)


class TestPunchStateMachine:
    """Security: validate that the state machine prevents invalid event sequences."""

    async def test_cannot_punch_out_without_punch_in(self, db, tenant, employee) -> None:
        with pytest.raises(InvalidPunchStateError, match="PUNCH_OUT"):
            await record_punch(
                db=db,
                tenant_id=tenant.id,
                employee_id=employee.id,
                event_type=EventType.PUNCH_OUT,
                source=EventSource.WEB_PORTAL,
                event_time=_dt(2025, 4, 1, 17, 0),
            )

    async def test_cannot_double_punch_in(self, db, tenant, employee) -> None:
        await record_punch(
            db=db,
            tenant_id=tenant.id,
            employee_id=employee.id,
            event_type=EventType.PUNCH_IN,
            source=EventSource.WEB_PORTAL,
            event_time=_dt(2025, 4, 2, 8, 0),
        )
        with pytest.raises(InvalidPunchStateError, match="PUNCH_IN"):
            await record_punch(
                db=db,
                tenant_id=tenant.id,
                employee_id=employee.id,
                event_type=EventType.PUNCH_IN,
                source=EventSource.WEB_PORTAL,
                event_time=_dt(2025, 4, 2, 8, 5),
            )

    async def test_cannot_break_start_without_punch_in(self, db, tenant, employee) -> None:
        with pytest.raises(InvalidPunchStateError, match="BREAK_START"):
            await record_punch(
                db=db,
                tenant_id=tenant.id,
                employee_id=employee.id,
                event_type=EventType.BREAK_START,
                source=EventSource.WEB_PORTAL,
                event_time=_dt(2025, 4, 3, 12, 0),
            )

    async def test_cannot_break_end_without_break_start(self, db, tenant, employee) -> None:
        await record_punch(
            db=db,
            tenant_id=tenant.id,
            employee_id=employee.id,
            event_type=EventType.PUNCH_IN,
            source=EventSource.WEB_PORTAL,
            event_time=_dt(2025, 4, 4, 8, 0),
        )
        with pytest.raises(InvalidPunchStateError, match="BREAK_END"):
            await record_punch(
                db=db,
                tenant_id=tenant.id,
                employee_id=employee.id,
                event_type=EventType.BREAK_END,
                source=EventSource.WEB_PORTAL,
                event_time=_dt(2025, 4, 4, 12, 30),
            )

    async def test_cannot_punch_out_during_break(self, db, tenant, employee) -> None:
        """Employee must end break before punching out."""
        await record_punch(
            db=db,
            tenant_id=tenant.id,
            employee_id=employee.id,
            event_type=EventType.PUNCH_IN,
            source=EventSource.WEB_PORTAL,
            event_time=_dt(2025, 4, 5, 8, 0),
        )
        await record_punch(
            db=db,
            tenant_id=tenant.id,
            employee_id=employee.id,
            event_type=EventType.BREAK_START,
            source=EventSource.WEB_PORTAL,
            event_time=_dt(2025, 4, 5, 12, 0),
        )
        with pytest.raises(InvalidPunchStateError, match="PUNCH_OUT"):
            await record_punch(
                db=db,
                tenant_id=tenant.id,
                employee_id=employee.id,
                event_type=EventType.PUNCH_OUT,
                source=EventSource.WEB_PORTAL,
                event_time=_dt(2025, 4, 5, 17, 0),
            )

    async def test_valid_full_day_sequence(self, db, tenant, employee) -> None:
        """Full valid sequence: IN -> BREAK_START -> BREAK_END -> OUT."""
        events = [
            (EventType.PUNCH_IN, _dt(2025, 4, 6, 8, 0)),
            (EventType.BREAK_START, _dt(2025, 4, 6, 12, 0)),
            (EventType.BREAK_END, _dt(2025, 4, 6, 12, 30)),
            (EventType.PUNCH_OUT, _dt(2025, 4, 6, 17, 0)),
        ]
        for etype, etime in events:
            await record_punch(
                db=db,
                tenant_id=tenant.id,
                employee_id=employee.id,
                event_type=etype,
                source=EventSource.WEB_PORTAL,
                event_time=etime,
            )
        # If we get here without exception, the sequence is valid

    async def test_can_punch_in_again_after_punch_out(self, db, tenant, employee) -> None:
        """Multiple sessions per day: OUT -> IN is valid."""
        events = [
            (EventType.PUNCH_IN, _dt(2025, 4, 7, 8, 0)),
            (EventType.PUNCH_OUT, _dt(2025, 4, 7, 12, 0)),
            (EventType.PUNCH_IN, _dt(2025, 4, 7, 13, 0)),
            (EventType.PUNCH_OUT, _dt(2025, 4, 7, 17, 0)),
        ]
        for etype, etime in events:
            await record_punch(
                db=db,
                tenant_id=tenant.id,
                employee_id=employee.id,
                event_type=etype,
                source=EventSource.WEB_PORTAL,
                event_time=etime,
            )


class TestPunchRateLimiting:
    """Security: prevent rapid-fire punch abuse."""

    async def test_rate_limit_rejects_fast_punches(self, db, tenant, employee) -> None:
        """Punches within 60 seconds are rejected (tested via _check_rate_limit directly)."""
        from zeiterfassung.services.clock_service import (
            _check_rate_limit,
        )

        # Record a punch with known time
        event = await record_punch(
            db=db,
            tenant_id=tenant.id,
            employee_id=employee.id,
            event_type=EventType.PUNCH_IN,
            source=EventSource.WEB_PORTAL,
            event_time=_dt(2025, 4, 10, 8, 0),
        )

        # Simulate same event type 30 seconds later -- should raise
        too_soon = _dt(2025, 4, 10, 8, 0, 30)
        with pytest.raises(PunchRateLimitError, match="60s"):
            _check_rate_limit(event, too_soon, EventType.PUNCH_IN)

        # Different event type within 30s -- should pass (no rate limit across types)
        _check_rate_limit(event, too_soon, EventType.BREAK_START)  # no exception

        # Same event type 90 seconds later -- should pass
        later = _dt(2025, 4, 10, 8, 1, 30)
        _check_rate_limit(event, later, EventType.PUNCH_IN)  # no exception

    async def test_rate_limit_allows_after_interval(self, db, tenant, employee) -> None:
        """Punches after 60+ seconds are allowed."""
        await record_punch(
            db=db,
            tenant_id=tenant.id,
            employee_id=employee.id,
            event_type=EventType.PUNCH_IN,
            source=EventSource.WEB_PORTAL,
            event_time=_dt(2025, 4, 11, 8, 0),
        )
        # 2 minutes later -- should be fine
        await record_punch(
            db=db,
            tenant_id=tenant.id,
            employee_id=employee.id,
            event_type=EventType.PUNCH_OUT,
            source=EventSource.WEB_PORTAL,
            event_time=_dt(2025, 4, 11, 8, 2),
        )

    async def test_rate_limit_skipped_for_admin_event_time(self, db, tenant, employee) -> None:
        """Admin corrections with explicit event_time bypass rate limiting."""
        await record_punch(
            db=db,
            tenant_id=tenant.id,
            employee_id=employee.id,
            event_type=EventType.PUNCH_IN,
            source=EventSource.ADMIN_MANUAL,
            event_time=_dt(2025, 4, 12, 8, 0),
        )
        # Admin sets a correction 10 seconds after -- allowed because event_time is explicit
        await record_punch(
            db=db,
            tenant_id=tenant.id,
            employee_id=employee.id,
            event_type=EventType.PUNCH_OUT,
            source=EventSource.ADMIN_MANUAL,
            event_time=_dt(2025, 4, 12, 8, 0, 10),
        )


class TestUndoLastPunch:
    """The one sanctioned delete path (NFC-terminal mis-tap recovery,
    UNDO_GRACE_SECONDS window). Also the path that sets the append-only
    escape-hatch GUC (migration 0036_append_only_triggers) — this exercises
    the dialect-guarded SET LOCAL on SQLite, where it must be a no-op."""

    async def test_undo_removes_the_event_within_grace_window(self, db, tenant, employee) -> None:
        await record_punch(
            db=db,
            tenant_id=tenant.id,
            employee_id=employee.id,
            event_type=EventType.PUNCH_IN,
            source=EventSource.NFC_TERMINAL,
            event_time=_dt(2025, 5, 1, 8, 0),
        )
        event_type, event_time = await undo_last_punch(db, tenant.id, employee.id)
        await db.commit()

        assert event_type == EventType.PUNCH_IN
        # SQLite round-trips DateTime(timezone=True) as offset-naive.
        assert event_time.replace(tzinfo=UTC) == _dt(2025, 5, 1, 8, 0)

        remaining = await db.execute(
            select(PunchEvent).where(
                PunchEvent.tenant_id == tenant.id,
                PunchEvent.employee_id == employee.id,
            )
        )
        assert remaining.scalars().all() == []

    async def test_undo_rejects_non_terminal_source(self, db, tenant, employee) -> None:
        await record_punch(
            db=db,
            tenant_id=tenant.id,
            employee_id=employee.id,
            event_type=EventType.PUNCH_IN,
            source=EventSource.WEB_PORTAL,
            event_time=_dt(2025, 5, 1, 8, 0),
        )
        with pytest.raises(NothingToUndoError):
            await undo_last_punch(db, tenant.id, employee.id)

    async def test_undo_rejects_after_grace_window_expires(self, db, tenant, employee) -> None:
        event = await record_punch(
            db=db,
            tenant_id=tenant.id,
            employee_id=employee.id,
            event_type=EventType.PUNCH_IN,
            source=EventSource.NFC_TERMINAL,
            event_time=_dt(2025, 5, 1, 8, 0),
        )
        event.created_at = datetime.now(UTC) - timedelta(seconds=UNDO_GRACE_SECONDS + 1)
        await db.commit()

        with pytest.raises(NothingToUndoError):
            await undo_last_punch(db, tenant.id, employee.id)

    async def test_undo_rejects_when_nothing_recorded(self, db, tenant, employee) -> None:
        with pytest.raises(NothingToUndoError):
            await undo_last_punch(db, tenant.id, employee.id)


class TestFruehesterBeginnNachtflag:
    """Die Profil-Regel ``fruehester_beginn`` kappt den Tagesbogen; das Nachtfenster
    muss dieselbe gewertete Zeit sehen. Sonst zahlt die Abrechnung einen
    Nachtzuschlag fuer Zeit, die gar nicht als Arbeit zaehlt."""

    @staticmethod
    def _profil(tenant, employee, fruehester_beginn: str | None) -> None:
        from sqlalchemy.orm.attributes import flag_modified

        settings = dict(tenant.settings or {})
        settings["wage_profiles"] = [
            {"id": "p-frueh", "name": "Fruehschicht", "fruehester_beginn": fruehester_beginn}
        ]
        tenant.settings = settings
        flag_modified(tenant, "settings")
        employee.wage_profile_id = "p-frueh"

    @staticmethod
    async def _schicht(db, tenant, employee, kommt: datetime, geht: datetime) -> None:
        for etype, etime in ((EventType.PUNCH_IN, kommt), (EventType.PUNCH_OUT, geht)):
            await record_punch(
                db=db,
                tenant_id=tenant.id,
                employee_id=employee.id,
                event_type=etype,
                source=EventSource.NFC_TERMINAL,
                event_time=etime,
            )

    async def test_gekappter_fruehstempel_loest_keinen_nachtzuschlag_aus(
        self, db, tenant, employee
    ) -> None:
        from datetime import date

        self._profil(tenant, employee, "06:00")
        # Lokal (Europe/Berlin, MEZ = UTC+1): 05:56 Kommen, 14:00 Gehen.
        await self._schicht(db, tenant, employee, _dt(2025, 3, 11, 4, 56), _dt(2025, 3, 11, 13, 0))

        ts = await recompute_daily_timesheet(db, tenant.id, employee.id, date(2025, 3, 11))
        assert ts.is_night_work is False
        # Gewertet ab 06:00 lokal: 8 h brutto minus Pflichtpause 30 min.
        assert ts.gross_work_minutes == 480
        assert ts.net_work_minutes == 450

    async def test_ohne_profil_bleibt_der_fruehstempel_nachtarbeit(
        self, db, tenant, employee
    ) -> None:
        from datetime import date

        await self._schicht(db, tenant, employee, _dt(2025, 3, 11, 4, 56), _dt(2025, 3, 11, 13, 0))

        ts = await recompute_daily_timesheet(db, tenant.id, employee.id, date(2025, 3, 11))
        assert ts.is_night_work is True
        assert ts.gross_work_minutes == 484

    async def test_echte_nachtarbeit_bleibt_erhalten(self, db, tenant, employee) -> None:
        from datetime import date

        self._profil(tenant, employee, "06:00")
        # Gegenprobe: Spaetschicht 22:00–23:30 lokal. Die Kappung greift hier gar
        # nicht, das Gehen faellt aber ins Nachtfenster (ab 23:00).
        await self._schicht(db, tenant, employee, _dt(2025, 3, 11, 21, 0), _dt(2025, 3, 11, 22, 30))

        ts = await recompute_daily_timesheet(db, tenant.id, employee.id, date(2025, 3, 11))
        assert ts.is_night_work is True

    async def test_nfc_arbeitszeit_profil_gewinnt_ueber_lohnprofil(
        self, db, tenant, employee
    ) -> None:
        """Fallback-Kette (Spec 2026-08-14): ein AKTIVES 'nfc'-Arbeitszeit-Profil
        kappt; die Lohnprofil-Regel ``fruehester_beginn`` greift nur noch ohne
        (aktives) Profil."""
        from datetime import date

        from zeiterfassung.models.work_time_profile import WorkTimeProfile, WorkTimeProfileKind

        self._profil(tenant, employee, "06:00")  # Lohnprofil wuerde ab 06:00 kappen
        profil = WorkTimeProfile(
            tenant_id=tenant.id,
            name="Fruehschicht ab 07:00",
            kind=WorkTimeProfileKind.NFC,
            is_active=True,
            clock_start="07:00",
            clock_end=None,
        )
        db.add(profil)
        await db.flush()
        employee.work_time_profile_id = profil.id
        await db.flush()

        # Lokal 05:56 Kommen, 14:00 Gehen — gewertet erst ab 07:00 (Profil),
        # nicht ab 06:00 (Lohnprofil): 7 h brutto, minus 30 min Pflichtpause.
        await self._schicht(db, tenant, employee, _dt(2025, 3, 11, 4, 56), _dt(2025, 3, 11, 13, 0))

        ts = await recompute_daily_timesheet(db, tenant.id, employee.id, date(2025, 3, 11))
        assert ts.gross_work_minutes == 420
        assert ts.net_work_minutes == 390
