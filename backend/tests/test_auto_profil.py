"""Auto-Arbeitszeit-Profile (kind 'auto', Spec 2026-08-14, Baustein C).

Ein zugewiesenes AKTIVES Auto-Profil gewinnt komplett: die Wochentags-Zeiten
bestimmen die Auto-Füllung (statt 07:00 + Soll + Pflichtpause), das Tages-Soll
(Fenster − Pause, mind. gesetzliche Pflichtpause) und das Anwesenheits-Fenster
inkl. ``on_break`` im Pausenfenster. Ohne (aktives) Profil bleibt ALLES exakt
wie heute — Regressionsschutz liegt in test_auto_capture / test_month_summary /
test_presence / test_overtime_daily.

Fixtage: Juli 2026 (Mo 6.7. .. Fr 10.7. sind Werktage, keine Feiertage HH);
Presence: Mo 10.8.2026 / Mi 12.8.2026, Europe/Berlin = UTC+2 (Sommerzeit).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from zeiterfassung.models.clock import DailyTimesheet, TimesheetStatus
from zeiterfassung.models.user import User
from zeiterfassung.models.work_time_profile import WorkTimeProfile, WorkTimeProfileKind
from zeiterfassung.policy import worktime
from zeiterfassung.services import clock_service
from zeiterfassung.services import overtime_service as ot

BERLIN = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 1, 1, tzinfo=UTC)

# Mo 06:00–14:30 mit Pause 12:00–12:30 (net 480), Di 07:00–15:00 OHNE
# Pausenfenster (Fenster 480 → gesetzliche Mindestpause 30 → net 450),
# Mi/Do/Fr/Sa/So frei.
WEEKDAYS_MO_DI = {
    "0": {"start": "06:00", "end": "14:30", "break_start": "12:00", "break_end": "12:30"},
    "1": {"start": "07:00", "end": "15:00"},
}


async def _auto_profil(
    db,
    tenant,
    employee,
    weekday_times: dict | None,
    *,
    is_active: bool = True,
) -> WorkTimeProfile:
    profil = WorkTimeProfile(
        tenant_id=tenant.id,
        name=f"Auto {uuid.uuid4().hex[:6]}",
        kind=WorkTimeProfileKind.AUTO,
        is_active=is_active,
        weekday_times=weekday_times,
    )
    db.add(profil)
    await db.flush()
    employee.time_capture = "auto"
    employee.work_time_profile_id = profil.id
    await db.flush()
    return profil


async def _rows(db, tenant, employee) -> dict[date, DailyTimesheet]:
    result = await db.execute(
        select(DailyTimesheet).where(
            DailyTimesheet.tenant_id == tenant.id,
            DailyTimesheet.employee_id == employee.id,
        )
    )
    return {r.work_date: r for r in result.scalars().all()}


# ---------------------------------------------------------------------------
# Policy-Helper (DB-frei): auto_day / auto_target_minutes
# ---------------------------------------------------------------------------


class TestPolicyHelpers:
    def _profil(self, *, kind="auto", is_active=True, weekday_times=WEEKDAYS_MO_DI):
        class P:
            pass

        p = P()
        p.kind = kind
        p.is_active = is_active
        p.weekday_times = weekday_times
        p.clock_start = None
        p.clock_end = None
        return p

    def test_kein_profil_greift_nicht(self):
        assert worktime.auto_target_minutes(None, 0) is None
        assert worktime.auto_day(None, 0) is None

    def test_nfc_profil_greift_nicht(self):
        p = self._profil(kind="nfc")
        assert worktime.auto_target_minutes(p, 0) is None

    def test_inaktives_profil_greift_nicht(self):
        p = self._profil(is_active=False)
        assert worktime.auto_target_minutes(p, 0) is None

    def test_tag_mit_zeiten_und_pausenfenster(self):
        d = worktime.auto_day(self._profil(), 0)
        assert d is not None
        assert (d.start_min, d.end_min) == (360, 870)
        assert (d.break_start_min, d.break_end_min) == (720, 750)
        assert d.break_minutes == 30
        assert d.target_minutes == 480

    def test_tag_ohne_pausenfenster_gesetzliche_mindestpause(self):
        # 07:00–15:00 = 480 min Fenster ohne Pause → 30 min Pflichtpause → 450.
        d = worktime.auto_day(self._profil(), 1)
        assert d is not None
        assert d.break_minutes == 30
        assert d.target_minutes == 450

    def test_freier_tag_soll_0_kein_fallback(self):
        # Profil greift, Tag frei → 0 (NICHT None: kein Fallback auf den Default).
        p = self._profil()
        assert worktime.auto_day(p, 2) is None
        assert worktime.auto_target_minutes(p, 2) == 0

    def test_weekday_times_komplett_none_ueberall_frei(self):
        p = self._profil(weekday_times=None)
        for wd in range(7):
            assert worktime.auto_target_minutes(p, wd) == 0


# ---------------------------------------------------------------------------
# ensure_auto_timesheets: Füllung folgt dem Profil
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fuellung_folgt_profil_zeiten_pause_net(db, tenant, employee):
    employee.counting_start_date = date(2026, 7, 1)
    await _auto_profil(db, tenant, employee, WEEKDAYS_MO_DI)

    await clock_service.ensure_auto_timesheets(
        db, tenant.id, employee.id, date(2026, 7, 6), date(2026, 7, 12), today=date(2026, 7, 12)
    )
    got = await _rows(db, tenant, employee)

    # Nur Mo 6.7. und Di 7.7. — Mi–Fr sind Profil-frei, Sa/So sowieso.
    assert set(got) == {date(2026, 7, 6), date(2026, 7, 7)}

    mo = got[date(2026, 7, 6)]
    assert mo.start_time == datetime.combine(date(2026, 7, 6), time(6, 0), tzinfo=BERLIN)
    assert mo.end_time == datetime.combine(date(2026, 7, 6), time(14, 30), tzinfo=BERLIN)
    assert mo.break_minutes == 30
    assert mo.net_work_minutes == 480
    assert mo.gross_work_minutes == 510
    assert mo.status == TimesheetStatus.COMPLETE

    # Di ohne Pausenfenster: gesetzliche Mindestpause 30 greift → net 450.
    di = got[date(2026, 7, 7)]
    assert di.start_time == datetime.combine(date(2026, 7, 7), time(7, 0), tzinfo=BERLIN)
    assert di.end_time == datetime.combine(date(2026, 7, 7), time(15, 0), tzinfo=BERLIN)
    assert di.break_minutes == 30
    assert di.net_work_minutes == 450
    assert di.gross_work_minutes == 480


@pytest.mark.asyncio
async def test_fuellung_idempotent_und_alter_bogen_wird_nachgezogen(db, tenant, employee):
    employee.counting_start_date = date(2026, 7, 1)
    await _auto_profil(db, tenant, employee, WEEKDAYS_MO_DI)
    # Alter Auto-Bogen (net>0, ohne Zeiten) am Mo → wird aufs Profil nachgezogen.
    db.add(
        DailyTimesheet(
            tenant_id=tenant.id,
            employee_id=employee.id,
            work_date=date(2026, 7, 6),
            net_work_minutes=480,
            gross_work_minutes=480,
            break_minutes=0,
            overtime_minutes=0,
            status=TimesheetStatus.COMPLETE,
            computed_from_events=False,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    # Manuell editierter Tag (Zeiten gesetzt) am Di → bleibt unangetastet.
    db.add(
        DailyTimesheet(
            tenant_id=tenant.id,
            employee_id=employee.id,
            work_date=date(2026, 7, 7),
            start_time=datetime.combine(date(2026, 7, 7), time(9, 0), tzinfo=BERLIN),
            end_time=datetime.combine(date(2026, 7, 7), time(13, 0), tzinfo=BERLIN),
            net_work_minutes=240,
            gross_work_minutes=240,
            break_minutes=0,
            overtime_minutes=0,
            status=TimesheetStatus.COMPLETE,
            computed_from_events=False,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    await db.flush()

    for _ in range(2):
        await clock_service.ensure_auto_timesheets(
            db, tenant.id, employee.id, date(2026, 7, 6), date(2026, 7, 12), today=date(2026, 7, 12)
        )
    got = await _rows(db, tenant, employee)
    assert set(got) == {date(2026, 7, 6), date(2026, 7, 7)}

    mo = got[date(2026, 7, 6)]
    assert mo.start_time == datetime.combine(date(2026, 7, 6), time(6, 0), tzinfo=BERLIN)
    assert mo.break_minutes == 30
    assert mo.net_work_minutes == 480

    di = got[date(2026, 7, 7)]
    assert di.net_work_minutes == 240, "manuell editierter Tag darf nicht überschrieben werden"


@pytest.mark.asyncio
async def test_inaktives_profil_fuellt_wie_ohne_profil(db, tenant, employee):
    employee.counting_start_date = date(2026, 7, 1)
    await _auto_profil(db, tenant, employee, WEEKDAYS_MO_DI, is_active=False)

    await clock_service.ensure_auto_timesheets(
        db, tenant.id, employee.id, date(2026, 7, 6), date(2026, 7, 12), today=date(2026, 7, 12)
    )
    got = await _rows(db, tenant, employee)

    # Default-Verhalten: Mo–Fr gefüllt, 07:00 + 8 h + 30 min Pflichtpause.
    assert set(got) == {date(2026, 7, d) for d in (6, 7, 8, 9, 10)}
    mo = got[date(2026, 7, 6)]
    assert mo.start_time == datetime.combine(date(2026, 7, 6), time(7, 0), tzinfo=BERLIN)
    assert mo.net_work_minutes == 480
    assert mo.break_minutes == 30


@pytest.mark.asyncio
async def test_profil_ohne_weekday_times_fuellt_nichts(db, tenant, employee):
    employee.counting_start_date = date(2026, 7, 1)
    await _auto_profil(db, tenant, employee, None)

    await clock_service.ensure_auto_timesheets(
        db, tenant.id, employee.id, date(2026, 7, 1), date(2026, 7, 31), today=date(2026, 7, 31)
    )
    assert await _rows(db, tenant, employee) == {}


# ---------------------------------------------------------------------------
# Tages-Soll: Konto (_daily_rows_for_month / month_summary) + recompute
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_konto_soll_folgt_profil_freie_tage_ohne_minus(db, tenant, employee):
    employee.counting_start_date = date(2026, 7, 1)
    await _auto_profil(db, tenant, employee, WEEKDAYS_MO_DI)

    await clock_service.ensure_auto_timesheets(
        db, tenant.id, employee.id, date(2026, 7, 1), date(2026, 7, 31), today=date(2026, 7, 11)
    )
    s = await ot.month_summary(db, tenant.id, employee, 2026, 7, today=date(2026, 7, 11))

    # Werktage 1.–10.7.: Mo 6.7. (480) + Di 7.7. (450) — Mi/Do/Fr sind laut
    # Profil frei (Soll 0, KEIN Minus; ohne Profil wären es 8 Werktage à 480).
    assert s["ist_minutes"] == 930
    assert s["soll_minutes"] == 930
    assert s["saldo_minutes"] == 0


@pytest.mark.asyncio
async def test_nicht_gearbeitet_marker_am_profil_freien_tag_kein_minus(db, tenant, employee):
    employee.counting_start_date = date(2026, 7, 1)
    await _auto_profil(db, tenant, employee, WEEKDAYS_MO_DI)
    # Expliziter "nicht gearbeitet"-Marker am Profil-freien Mi 8.7. — ohne
    # Profil zählte der leere Bogen als unentschuldigtes Minus (−480).
    db.add(
        DailyTimesheet(
            tenant_id=tenant.id,
            employee_id=employee.id,
            work_date=date(2026, 7, 8),
            net_work_minutes=0,
            gross_work_minutes=0,
            break_minutes=0,
            overtime_minutes=0,
            status=TimesheetStatus.COMPLETE,
            computed_from_events=False,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    await db.flush()

    s = await ot.month_summary(db, tenant.id, employee, 2026, 7, today=date(2026, 7, 11))
    assert s["soll_minutes"] == 0
    assert s["saldo_minutes"] == 0


@pytest.mark.asyncio
async def test_nicht_gearbeitet_marker_am_profil_tag_zaehlt_minus(db, tenant, employee):
    employee.counting_start_date = date(2026, 7, 6)
    await _auto_profil(db, tenant, employee, WEEKDAYS_MO_DI)
    # Marker am Profil-Tag Mo 6.7. (Soll 480) → Minus in Profil-Höhe.
    db.add(
        DailyTimesheet(
            tenant_id=tenant.id,
            employee_id=employee.id,
            work_date=date(2026, 7, 6),
            net_work_minutes=0,
            gross_work_minutes=0,
            break_minutes=0,
            overtime_minutes=0,
            status=TimesheetStatus.COMPLETE,
            computed_from_events=False,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    await db.flush()

    s = await ot.month_summary(db, tenant.id, employee, 2026, 7, today=date(2026, 7, 7))
    assert s["soll_minutes"] == 480
    assert s["saldo_minutes"] == -480


@pytest.mark.asyncio
async def test_recompute_overtime_nutzt_profil_soll(db, tenant, employee):
    from zeiterfassung.models.clock import EventSource, EventType

    employee.contracted_hours_per_week = 20.0  # Default-Soll wäre 240
    await _auto_profil(db, tenant, employee, WEEKDAYS_MO_DI)

    # Mo 6.7.: 06:00–15:30 lokal (04:00–13:30 UTC) ohne Pausen-Stempel →
    # Anwesenheit 570, Pflichtpause 30, net 540. Profil-Soll Mo = 480 →
    # Überstunden 60 (mit Default-Soll 240 wären es 300).
    for etype, hh, mm in ((EventType.PUNCH_IN, 4, 0), (EventType.PUNCH_OUT, 13, 30)):
        await clock_service.record_punch(
            db=db,
            tenant_id=tenant.id,
            employee_id=employee.id,
            event_type=etype,
            source=EventSource.NFC_TERMINAL,
            event_time=datetime(2026, 7, 6, hh, mm, tzinfo=UTC),
        )
    ts = await clock_service.recompute_daily_timesheet(db, tenant.id, employee.id, date(2026, 7, 6))
    assert ts.net_work_minutes == 540
    assert ts.overtime_minutes == 60


# ---------------------------------------------------------------------------
# Anwesenheit (get_presence_overview, Auto-Zweig)
# ---------------------------------------------------------------------------


class TestAutoProfilPresence:
    """Fenster + Pausenfenster aus dem Profil; Fixtage Mo 10.8. / Mi 12.8.2026."""

    def _scope(self, tenant_id):
        from zeiterfassung.api.deps import ScopeContext

        return ScopeContext(scope="global", tenant_id=tenant_id, user_id=None)

    async def _mk_user(self, db, tenant) -> User:
        from zeiterfassung.core.security import hash_password

        now = datetime.now(tz=UTC)
        u = User(
            tenant_id=tenant.id,
            email=f"auto-{uuid.uuid4().hex[:8]}@test.de",
            hashed_password=hash_password("test123"),
            first_name="Auto",
            last_name="Profil",
            employee_number=f"EMP{uuid.uuid4().hex[:5]}",
            contracted_hours_per_week=40.0,
            time_capture="auto",
            is_active=True,
            created_at=now,
            updated_at=now,
        )
        db.add(u)
        await db.flush()
        return u

    async def _overview(self, db, tenant, now):
        from zeiterfassung.services.clock_service import get_presence_overview

        return await get_presence_overview(db, tenant.id, self._scope(tenant.id), now=now)

    async def _status_of(self, db, tenant, uid, now):
        data = await self._overview(db, tenant, now)
        return {e["employee_id"]: e for e in data["employees"]}[uid]

    async def test_im_profil_fenster_anwesend(self, db, tenant, employee):
        # Mo 10.8., 10:00 lokal (08:00 UTC) — im Fenster 06:00–14:30.
        u = await self._mk_user(db, tenant)
        await _auto_profil(db, tenant, u, WEEKDAYS_MO_DI)
        e = await self._status_of(db, tenant, u.id, datetime(2026, 8, 10, 8, 0, tzinfo=UTC))
        assert e["status"] == "clocked_in"
        assert e["clocked_in_since"] is not None

    async def test_vor_profil_fenster_abwesend_trotz_default_fenster(self, db, tenant, employee):
        # Mo 06:30 lokal (04:30 UTC): im Profil-Fenster (ab 06:00) anwesend —
        # das Default-Fenster (ab 07:00) hätte "abwesend" gesagt. Und 05:30
        # lokal ist auch mit Profil noch abwesend.
        u = await self._mk_user(db, tenant)
        await _auto_profil(db, tenant, u, WEEKDAYS_MO_DI)
        e = await self._status_of(db, tenant, u.id, datetime(2026, 8, 10, 4, 30, tzinfo=UTC))
        assert e["status"] == "clocked_in"
        e = await self._status_of(db, tenant, u.id, datetime(2026, 8, 10, 3, 30, tzinfo=UTC))
        assert e["status"] == "clocked_out"

    async def test_nach_profil_fenster_abwesend(self, db, tenant, employee):
        # Mo 15:00 lokal (13:00 UTC) — nach 14:30 → nicht mehr anwesend
        # (Default-Fenster 07:00 + Soll + Pause ginge bis 15:30).
        u = await self._mk_user(db, tenant)
        await _auto_profil(db, tenant, u, WEEKDAYS_MO_DI)
        e = await self._status_of(db, tenant, u.id, datetime(2026, 8, 10, 13, 0, tzinfo=UTC))
        assert e["status"] == "clocked_out"

    async def test_im_pausenfenster_on_break(self, db, tenant, employee):
        # Mo 12:15 lokal (10:15 UTC) — im Pausenfenster 12:00–12:30 → on_break.
        u = await self._mk_user(db, tenant)
        await _auto_profil(db, tenant, u, WEEKDAYS_MO_DI)
        data = await self._overview(db, tenant, datetime(2026, 8, 10, 10, 15, tzinfo=UTC))
        e = {x["employee_id"]: x for x in data["employees"]}[u.id]
        assert e["status"] == "on_break"
        assert data["on_break_count"] == 1
        assert data["present_count"] == 1

    async def test_profil_freier_tag_abwesend(self, db, tenant, employee):
        # Mi 12.8., 12:00 lokal (10:00 UTC): Profil kennt nur Mo/Di → abwesend,
        # obwohl das Default-Fenster (07:00–15:30) "anwesend" sagen würde.
        u = await self._mk_user(db, tenant)
        await _auto_profil(db, tenant, u, WEEKDAYS_MO_DI)
        e = await self._status_of(db, tenant, u.id, datetime(2026, 8, 12, 10, 0, tzinfo=UTC))
        assert e["status"] == "clocked_out"

    async def test_inaktives_profil_wie_ohne_profil(self, db, tenant, employee):
        # Inaktives Profil → Default-Fenster: Mi 12:00 lokal anwesend.
        u = await self._mk_user(db, tenant)
        await _auto_profil(db, tenant, u, WEEKDAYS_MO_DI, is_active=False)
        e = await self._status_of(db, tenant, u.id, datetime(2026, 8, 12, 10, 0, tzinfo=UTC))
        assert e["status"] == "clocked_in"

    async def test_tagesbogen_fenster_gewinnt_gegen_profil(self, db, tenant, employee):
        # Manuell angepasster Tag Mo 16:00–20:00 lokal → um 10:00 lokal NICHT
        # anwesend (obwohl im Profil-Fenster), um 17:00 lokal schon.
        u = await self._mk_user(db, tenant)
        await _auto_profil(db, tenant, u, WEEKDAYS_MO_DI)
        db.add(
            DailyTimesheet(
                tenant_id=tenant.id,
                employee_id=u.id,
                work_date=date(2026, 8, 10),
                start_time=datetime(2026, 8, 10, 14, 0, tzinfo=UTC),
                end_time=datetime(2026, 8, 10, 18, 0, tzinfo=UTC),
                break_minutes=0,
                net_work_minutes=240,
                gross_work_minutes=240,
                overtime_minutes=0,
                status=TimesheetStatus.COMPLETE,
                computed_from_events=False,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        await db.flush()
        now_in_profile = datetime(2026, 8, 10, 8, 0, tzinfo=UTC)
        now_in_sheet = datetime(2026, 8, 10, 15, 0, tzinfo=UTC)
        assert (await self._status_of(db, tenant, u.id, now_in_profile))["status"] == "clocked_out"
        assert (await self._status_of(db, tenant, u.id, now_in_sheet))["status"] == "clocked_in"
