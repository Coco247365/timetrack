"""Stempel-Kappung nach Arbeitszeit-Profil (kind 'nfc') — Spec 2026-08-14, Baustein B.

Konto (``recompute_daily_timesheet``), Export-Segmente (``payroll/rules``) und
die Tagesansicht (``get_day_events``: ``counted_time``) werten Stempel nur
innerhalb des erlaubten Fensters ``clock_start``–``clock_end`` (lokale
Mandanten-Zeit, ggf. ueber Mitternacht). Die Roh-Events bleiben unangetastet
(append-only, BetrVG); Fallback ohne (aktives) NFC-Profil ist die
Lohnprofil-Regel ``fruehester_beginn`` (Bestandstests in test_clock.py).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select

from zeiterfassung.models.clock import EventSource, EventType, PunchEvent
from zeiterfassung.models.work_time_profile import WorkTimeProfile, WorkTimeProfileKind
from zeiterfassung.payroll.rules import WorkSegment, clip_segments_window
from zeiterfassung.policy.worktime import Window, counted_minute_of_day, parse_hhmm
from zeiterfassung.services.clock_service import (
    get_day_events,
    recompute_daily_timesheet,
    record_punch,
)


def _dt(day: int, hhmm: str) -> datetime:
    """Lokale Berliner Zeit (Maerz 2026 = MEZ, UTC+1) -> UTC (tz-aware)."""
    h, m = map(int, hhmm.split(":"))
    return datetime(2026, 3, day, tzinfo=UTC) + timedelta(minutes=h * 60 + m - 60)


# ---------------------------------------------------------------------------
# Reine Fenster-Logik (policy/worktime.py) — keine DB.
# ---------------------------------------------------------------------------


class TestWorktimeFenster:
    def test_parse_hhmm(self) -> None:
        assert parse_hhmm("06:00") == 360
        assert parse_hhmm("22:15") == 22 * 60 + 15
        assert parse_hhmm(None) is None
        assert parse_hhmm("") is None

    def test_over_midnight_property(self) -> None:
        assert Window(22 * 60, 6 * 60 + 15).over_midnight is True
        assert Window(6 * 60, 18 * 60).over_midnight is False
        assert Window(22 * 60, None).over_midnight is False
        assert Window(None, 6 * 60).over_midnight is False

    def test_normalfenster_klemmt_beide_seiten(self) -> None:
        w = Window(360, 1080)  # 06:00–18:00
        assert counted_minute_of_day(300, w) == 360  # davor -> start
        assert counted_minute_of_day(360, w) == 360
        assert counted_minute_of_day(700, w) == 700  # im Fenster
        assert counted_minute_of_day(1080, w) == 1080
        assert counted_minute_of_day(1100, w) == 1080  # danach -> end

    def test_nur_eine_seite_klemmt_nur_diese(self) -> None:
        nur_start = Window(360, None)
        assert counted_minute_of_day(300, nur_start) == 360
        assert counted_minute_of_day(1439, nur_start) == 1439
        nur_ende = Window(None, 960)
        assert counted_minute_of_day(1000, nur_ende) == 960
        assert counted_minute_of_day(100, nur_ende) == 100

    def test_ueber_mitternacht_luecke_klemmt_zur_naeheren_grenze(self) -> None:
        w = Window(1320, 375)  # 22:00–06:15
        # Erlaubt: [00:00, 06:15] und [22:00, 24:00).
        assert counted_minute_of_day(60, w) == 60
        assert counted_minute_of_day(375, w) == 375
        assert counted_minute_of_day(1320, w) == 1320
        assert counted_minute_of_day(1330, w) == 1330
        # In der Luecke: naeher an end -> end, sonst -> start.
        assert counted_minute_of_day(380, w) == 375
        assert counted_minute_of_day(1310, w) == 1320
        # Mittelpunkt (375+1320)/2 = 847.5: < Mittelpunkt -> end, sonst -> start.
        assert counted_minute_of_day(847, w) == 375
        assert counted_minute_of_day(848, w) == 1320


class TestClipSegmentsWindow:
    """Fenster-Kappung fuer Export/Stundenzettel-Segmente (naive UTC + Offset)."""

    def test_fensterende_kappt_das_segmentende(self) -> None:
        w = Window(360, 960)  # 06:00–16:00, MEZ-Offset 60
        seg = WorkSegment(
            start=datetime(2026, 3, 10, 7, 0),  # 08:00 lokal
            end=datetime(2026, 3, 10, 15, 30),  # 16:30 lokal
        )
        [clipped] = clip_segments_window([seg], w, tz_offset_min=60)
        assert clipped.start == seg.start
        assert clipped.end == datetime(2026, 3, 10, 15, 0)  # 16:00 lokal

    def test_ueber_mitternacht_fenster_klemmt_beide_seiten(self) -> None:
        w = Window(22 * 60, 6 * 60 + 15)  # 22:00–06:15
        seg = WorkSegment(
            start=datetime(2026, 3, 10, 20, 45),  # 21:45 lokal
            end=datetime(2026, 3, 11, 5, 20),  # 06:20 lokal (Folgetag)
        )
        [clipped] = clip_segments_window([seg], w, tz_offset_min=60)
        assert clipped.start == datetime(2026, 3, 10, 21, 0)  # 22:00 lokal
        assert clipped.end == datetime(2026, 3, 11, 5, 15)  # 06:15 lokal

    def test_segment_komplett_in_der_luecke_entfaellt(self) -> None:
        w = Window(22 * 60, 6 * 60 + 15)
        seg = WorkSegment(
            start=datetime(2026, 3, 10, 9, 0),  # 10:00 lokal
            end=datetime(2026, 3, 10, 13, 0),  # 14:00 lokal
        )
        assert clip_segments_window([seg], w, tz_offset_min=60) == []

    def test_einseitiges_fenster_verlaengert_nie(self) -> None:
        # Nur-Beginn-Fenster + Ueber-Mitternacht-Segment: das Ende (02:00) liegt
        # zwar vor 06:00, darf aber nicht auf 06:00 des Folgetags RUTSCHEN —
        # Kappung verkuerzt nur, sie erfindet keine Zeit.
        w = Window(360, None)
        seg = WorkSegment(
            start=datetime(2026, 3, 10, 21, 0),  # 22:00 lokal
            end=datetime(2026, 3, 11, 1, 0),  # 02:00 lokal (Folgetag)
        )
        [clipped] = clip_segments_window([seg], w, tz_offset_min=60)
        assert clipped.start == seg.start
        assert clipped.end == seg.end


# ---------------------------------------------------------------------------
# Konto: recompute_daily_timesheet kappt nach dem NFC-Profil-Fenster.
# ---------------------------------------------------------------------------


async def _nfc_profil(
    db,
    tenant,
    employee,
    *,
    clock_start: str | None,
    clock_end: str | None,
    is_active: bool = True,
) -> WorkTimeProfile:
    profil = WorkTimeProfile(
        tenant_id=tenant.id,
        name=f"Fenster {uuid.uuid4().hex[:6]}",
        kind=WorkTimeProfileKind.NFC,
        is_active=is_active,
        clock_start=clock_start,
        clock_end=clock_end,
    )
    db.add(profil)
    await db.flush()
    employee.work_time_profile_id = profil.id
    await db.flush()
    return profil


async def _stempeln(db, tenant, employee, events: list[tuple[EventType, datetime]]) -> None:
    for etype, etime in events:
        await record_punch(
            db=db,
            tenant_id=tenant.id,
            employee_id=employee.id,
            event_type=etype,
            source=EventSource.NFC_TERMINAL,
            event_time=etime,
        )


class TestKontoKappung:
    async def test_einstempeln_vor_fensterbeginn_zaehlt_ab_beginn(
        self, db, tenant, employee
    ) -> None:
        await _nfc_profil(db, tenant, employee, clock_start="06:00", clock_end="18:00")
        await _stempeln(
            db,
            tenant,
            employee,
            [(EventType.PUNCH_IN, _dt(10, "05:55")), (EventType.PUNCH_OUT, _dt(10, "14:00"))],
        )

        ts = await recompute_daily_timesheet(db, tenant.id, employee.id, date(2026, 3, 10))
        # Gewertet ab 06:00 lokal: 8 h brutto, minus 30 min Pflichtpause.
        assert ts.gross_work_minutes == 480
        assert ts.net_work_minutes == 450

        # Roh-Event bleibt unangetastet (append-only).
        raw = (
            (
                await db.execute(
                    select(PunchEvent)
                    .where(
                        PunchEvent.tenant_id == tenant.id,
                        PunchEvent.employee_id == employee.id,
                        PunchEvent.event_type == EventType.PUNCH_IN,
                    )
                    .order_by(PunchEvent.event_time)
                )
            )
            .scalars()
            .first()
        )
        assert raw is not None
        assert raw.event_time == _dt(10, "05:55")

    async def test_ausstempeln_nach_fensterende_wird_gekappt(self, db, tenant, employee) -> None:
        await _nfc_profil(db, tenant, employee, clock_start="06:00", clock_end="16:00")
        await _stempeln(
            db,
            tenant,
            employee,
            [(EventType.PUNCH_IN, _dt(10, "08:00")), (EventType.PUNCH_OUT, _dt(10, "16:30"))],
        )

        ts = await recompute_daily_timesheet(db, tenant.id, employee.id, date(2026, 3, 10))
        # Gewertet bis 16:00 lokal: 8 h brutto, minus 30 min Pflichtpause.
        assert ts.gross_work_minutes == 480
        assert ts.net_work_minutes == 450

    async def test_pausenstempel_vor_fensterbeginn_werden_geklemmt(
        self, db, tenant, employee
    ) -> None:
        await _nfc_profil(db, tenant, employee, clock_start="06:00", clock_end=None)
        await _stempeln(
            db,
            tenant,
            employee,
            [
                (EventType.PUNCH_IN, _dt(10, "05:00")),
                (EventType.BREAK_START, _dt(10, "05:15")),
                (EventType.BREAK_END, _dt(10, "05:45")),
                (EventType.PUNCH_OUT, _dt(10, "12:00")),
            ],
        )

        ts = await recompute_daily_timesheet(db, tenant.id, employee.id, date(2026, 3, 10))
        # Alles vor 06:00 klemmt auf 06:00: die Fruehpause kollabiert zu 0 min,
        # gewertet wird 06:00–12:00 (6 h, keine Pflichtpause noetig).
        assert ts.break_minutes == 0
        assert ts.gross_work_minutes == 360
        assert ts.net_work_minutes == 360

    async def test_nur_fensterende_kappt_nur_das_ende(self, db, tenant, employee) -> None:
        await _nfc_profil(db, tenant, employee, clock_start=None, clock_end="16:00")
        await _stempeln(
            db,
            tenant,
            employee,
            [(EventType.PUNCH_IN, _dt(10, "08:00")), (EventType.PUNCH_OUT, _dt(10, "16:30"))],
        )

        ts = await recompute_daily_timesheet(db, tenant.id, employee.id, date(2026, 3, 10))
        assert ts.gross_work_minutes == 480
        assert ts.net_work_minutes == 450

    async def test_inaktives_profil_kappt_nicht(self, db, tenant, employee) -> None:
        await _nfc_profil(
            db, tenant, employee, clock_start="06:00", clock_end="18:00", is_active=False
        )
        await _stempeln(
            db,
            tenant,
            employee,
            [(EventType.PUNCH_IN, _dt(10, "05:00")), (EventType.PUNCH_OUT, _dt(10, "12:00"))],
        )

        ts = await recompute_daily_timesheet(db, tenant.id, employee.id, date(2026, 3, 10))
        # 05:00–12:00 lokal = 7 h brutto, minus 30 min Pflichtpause.
        assert ts.gross_work_minutes == 420
        assert ts.net_work_minutes == 390


# ---------------------------------------------------------------------------
# Tagesansicht: get_day_events liefert je Event zusaetzlich counted_time.
# ---------------------------------------------------------------------------


class TestDayEventsCountedTime:
    async def test_counted_time_zeigt_die_gewertete_zeit(self, db, tenant, employee) -> None:
        await _nfc_profil(db, tenant, employee, clock_start="06:00", clock_end="18:00")
        await _stempeln(
            db,
            tenant,
            employee,
            [(EventType.PUNCH_IN, _dt(10, "05:55")), (EventType.PUNCH_OUT, _dt(10, "14:00"))],
        )

        events = await get_day_events(db, tenant.id, employee.id, date(2026, 3, 10))
        ein = next(e for e in events if e["event_type"] == "PUNCH_IN")
        aus = next(e for e in events if e["event_type"] == "PUNCH_OUT")
        # Roh 05:55 lokal (04:55 UTC), gewertet ab 06:00 lokal (05:00 UTC).
        assert ein["event_time"] == "2026-03-10T04:55:00+00:00"
        assert ein["counted_time"] == "2026-03-10T05:00:00+00:00"
        # Keine Kappung -> counted_time == event_time.
        assert aus["counted_time"] == aus["event_time"]

        # Das Response-Schema kennt das Feld (DayDetail der Tagesansicht).
        from zeiterfassung.schemas.clock import DayEventEntry

        entry = DayEventEntry(**ein)
        assert entry.counted_time == datetime(2026, 3, 10, 5, 0, tzinfo=UTC)

    async def test_counted_time_ueber_mitternacht(self, db, tenant, employee) -> None:
        await _nfc_profil(db, tenant, employee, clock_start="22:00", clock_end="06:15")
        await _stempeln(
            db,
            tenant,
            employee,
            [(EventType.PUNCH_IN, _dt(10, "21:45")), (EventType.PUNCH_OUT, _dt(11, "06:20"))],
        )

        tag1 = await get_day_events(db, tenant.id, employee.id, date(2026, 3, 10))
        ein = next(e for e in tag1 if e["event_type"] == "PUNCH_IN")
        assert ein["counted_time"] == _dt(10, "22:00").isoformat()

        tag2 = await get_day_events(db, tenant.id, employee.id, date(2026, 3, 11))
        aus = next(e for e in tag2 if e["event_type"] == "PUNCH_OUT")
        assert aus["counted_time"] == _dt(11, "06:15").isoformat()

    async def test_ohne_kappung_ist_counted_time_die_stempelzeit(
        self, db, tenant, employee
    ) -> None:
        await _stempeln(
            db,
            tenant,
            employee,
            [(EventType.PUNCH_IN, _dt(10, "08:00")), (EventType.PUNCH_OUT, _dt(10, "16:00"))],
        )
        events = await get_day_events(db, tenant.id, employee.id, date(2026, 3, 10))
        assert all(e["counted_time"] == e["event_time"] for e in events)


# ---------------------------------------------------------------------------
# Export (DATEV, Soll-Basis): die Kappung nutzt auch das FENSTERENDE —
# im Stil von test_export_profil_regeln.py (dort kappt nur der Beginn).
# ---------------------------------------------------------------------------


def _utc_juni(day: int, hhmm: str) -> datetime:
    """Lokale Berliner Zeit (Juni 2026 = CEST, UTC+2) -> UTC."""
    h, m = map(int, hhmm.split(":"))
    return datetime(2026, 6, day, tzinfo=UTC) + timedelta(minutes=h * 60 + m - 120)


class TestExportFensterKappung:
    WAGE_TYPES = {
        "NORMALE_ARBEITSZEIT": 11,
        "MEHRARBEIT_0": 62,
        "MEHRARBEIT_ZUSCHLAG": 66,
        "MINUSSTUNDEN": 13,
        "NACHT_ARBEIT": 84,
    }

    async def _punch(self, db, tenant, employee, day: int, kommt: str, geht: str, pause: int):
        now = datetime.now(UTC)
        events: list[tuple[str, str]] = [(EventType.PUNCH_IN, kommt)]
        if pause > 0:
            ende = 12 * 60 + pause
            events.append((EventType.BREAK_START, "12:00"))
            events.append((EventType.BREAK_END, f"{ende // 60}:{ende % 60:02d}"))
        events.append((EventType.PUNCH_OUT, geht))
        for etype, t in events:
            db.add(
                PunchEvent(
                    tenant_id=tenant.id,
                    employee_id=employee.id,
                    event_type=etype,
                    event_time=_utc_juni(day, t),
                    source=EventSource.NFC_TERMINAL,
                    created_at=now,
                )
            )

    async def test_kappung_nutzt_das_fensterende(self, db, tenant, employee) -> None:
        from zeiterfassung.models.export import WageTypeMapping
        from zeiterfassung.services.export_service import _build_datev_rows

        tenant.settings = {
            **(tenant.settings or {}),
            "zuschlaege": {"export_basis": "soll", "mehrarbeit_frei_wochen_std": 1.5},
        }
        now = datetime.now(UTC)
        for nr in self.WAGE_TYPES.values():
            db.add(
                WageTypeMapping(
                    tenant_id=tenant.id,
                    internal_code=f"LA_{nr:04d}",
                    datev_lohnart=nr,
                    description=f"LA {nr}",
                    is_active=True,
                    unit="hours",
                    created_at=now,
                )
            )
        employee.contracted_hours_per_week = 38.5
        employee.weekday_target_minutes = [495, 495, 495, 495, 330, 0, 0]
        employee.employment_type = "hourly"
        employee.overtime_payout_mode = "all"
        await _nfc_profil(db, tenant, employee, clock_start="06:00", clock_end="16:00")

        # Alle uebrigen Werktage exakt auf Tagessoll punchen (innerhalb des
        # Fensters), damit nur der 01.06. das Ergebnis beeinflusst.
        weekday_target = {0: 495, 1: 495, 2: 495, 3: 495, 4: 330}
        for day in range(2, 31):
            wd = date(2026, 6, day).weekday()
            if wd not in weekday_target:
                continue
            geht_min = 6 * 60 + weekday_target[wd]
            await self._punch(
                db, tenant, employee, day, "06:00", f"{geht_min // 60}:{geht_min % 60:02d}", 0
            )
        # 01.06. (Montag): 05:20–16:35 gestempelt, 45 min Pause. Gewertet wird
        # NUR 06:00–16:00 (Fenster): 10 h minus 45 min Pause = 9:15 -> 1 h
        # Mehrarbeit ueber dem Tagessoll 8:15; die Nachtminuten 05:20–06:00
        # entfallen komplett.
        await self._punch(db, tenant, employee, 1, "05:20", "16:35", 45)
        await db.commit()

        rows = await _build_datev_rows(db, tenant.id, 2026, 6, [employee.id], self.WAGE_TYPES)
        by_lohnart: dict[int, float] = {}
        for r in rows:
            by_lohnart[r.lohnart] = round(by_lohnart.get(r.lohnart, 0.0) + r.wert, 2)

        assert 84 not in by_lohnart, "gekappte Fruehminuten duerfen keine Nachtarbeit ausloesen"
        assert 13 not in by_lohnart, "mit Ende-Kappung entstehen keine Minusstunden"
        # Ohne Ende-Kappung waere die Mehrarbeit 1,5 h (bis 16:35) — mit
        # Fensterende 16:00 ist es genau 1,0 h, komplett im Freikontingent.
        assert by_lohnart.get(62) == 1.0
        assert 66 not in by_lohnart
