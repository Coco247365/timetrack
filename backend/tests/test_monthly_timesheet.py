"""Monats-Stundenzettel pro Mitarbeiter (GET /admin/monthly-timesheet).

Prueft die Tagesrechnung (Segmente in lokaler Zeit, Pause, Ist/Soll/Diff),
Abwesenheits-Label + Summenblock sowie die Berechtigungspruefung (403 ohne
passende Rechte). Berechnung analog zum Soll-Modus des DATEV-Exports.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from zeiterfassung.core.security import create_access_token, hash_password
from zeiterfassung.models.absence import RequestType
from zeiterfassung.models.clock import EventSource, EventType, PunchEvent
from zeiterfassung.models.user import User
from zeiterfassung.policy.context import DayPart
from zeiterfassung.schemas.absence import AbsenceRequestCreate
from zeiterfassung.services.absence_service import create_request


@pytest_asyncio.fixture
async def app_with_db(engine):
    from zeiterfassung.api import deps
    from zeiterfassung.main import create_app

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async def override_get_db():
        async with factory() as session:
            yield session

    app = create_app()
    app.dependency_overrides[deps.get_db] = override_get_db
    yield app
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client(app_with_db):
    async with AsyncClient(transport=ASGITransport(app=app_with_db), base_url="http://test") as c:
        yield c


async def _gf_token(db: AsyncSession, tenant_id: uuid.UUID) -> str:
    from tests.conftest import ensure_login_account

    acc = await ensure_login_account(db, tenant_id)
    return create_access_token(
        user_id=str(acc.id),
        tenant_id=str(tenant_id),
        permissions=["timesheet:view_all", "admin:dashboard"],
        department_id=None,
        role_type="geschaeftsfuehrung",
        data_scope="global",
    )


async def _no_perm_token(db: AsyncSession, tenant_id: uuid.UUID) -> str:
    from tests.conftest import ensure_login_account

    acc = await ensure_login_account(db, tenant_id)
    return create_access_token(
        user_id=str(acc.id),
        tenant_id=str(tenant_id),
        permissions=["timesheet:view_own"],
        department_id=None,
        role_type="mitarbeiter",
        data_scope="self",
    )


@pytest_asyncio.fixture
async def plan_employee(db, tenant) -> User:
    """Mitarbeiter mit Wochentagsplan Mo–Do 8:15 (495), Fr 5:30 (330)."""
    now = datetime.now(tz=UTC)
    u = User(
        tenant_id=tenant.id,
        email="plan@test.de",
        hashed_password=hash_password("Passw0rd!"),
        first_name="Paula",
        last_name="Plan",
        employee_number="PLAN1",
        contracted_hours_per_week=38.5,
        weekday_target_minutes=[495, 495, 495, 495, 330, 0, 0],
        employment_type="hourly",
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    db.add(u)
    await db.flush()
    return u


def _utc(day: int, hhmm: str) -> datetime:
    """Lokale Berliner Zeit (Juni = CEST, UTC+2) -> UTC."""
    h, m = map(int, hhmm.split(":"))
    local_minutes = h * 60 + m - 120
    return datetime(2026, 6, day, local_minutes // 60, local_minutes % 60, tzinfo=UTC)


async def _recompute(db, tenant, employee, *days: int) -> None:
    """Tagesboegen nachrechnen — wie es die Produktion bei jedem Stempel tut.

    Der Zettel liest sein Ist aus dem Tagesbogen (eine Quelle); Tests, die
    rohe Events einwerfen, muessen den Bogen genauso nachziehen.
    """
    from zeiterfassung.services import clock_service

    for d in days:
        await clock_service.recompute_daily_timesheet(db, tenant.id, employee.id, date(2026, 6, d))


class TestMonthlyTimesheet:
    async def test_tagesrechnung_absence_und_summary(
        self, db, client, tenant, plan_employee
    ) -> None:
        now = datetime.now(UTC)
        # Mo 01.06. 06:31–17:30 lokal mit Pause 12:00–12:45 -> Ist 10:14 (614).
        # Di 02.06. 06:00–14:15 lokal ohne Pause -> Ist 8:15 (495) = Soll.
        punches = [
            (1, EventType.PUNCH_IN, "06:31"),
            (1, EventType.BREAK_START, "12:00"),
            (1, EventType.BREAK_END, "12:45"),
            (1, EventType.PUNCH_OUT, "17:30"),
            (2, EventType.PUNCH_IN, "06:00"),
            (2, EventType.PUNCH_OUT, "14:15"),
        ]
        for day, etype, t in punches:
            db.add(
                PunchEvent(
                    tenant_id=tenant.id,
                    employee_id=plan_employee.id,
                    event_type=etype,
                    event_time=_utc(day, t),
                    source=EventSource.NFC_TERMINAL,
                    created_at=now,
                )
            )
        # Mi 03.06. Urlaub (ganztags, genehmigt).
        vac = AbsenceRequestCreate.model_construct(
            request_type=RequestType.VACATION,
            date_from=date(2026, 6, 3),
            date_to=date(2026, 6, 3),
            day_part=DayPart.FULL,
            hours=None,
            sick_status=None,
            payload=None,
            notes=None,
        )
        await create_request(
            db, tenant.id, plan_employee.id, vac, "HH", auto_approve=True, approver_id=uuid.uuid4()
        )
        await _recompute(db, tenant, plan_employee, 1, 2)
        await db.commit()

        r = await client.get(
            "/api/v1/admin/monthly-timesheet",
            params={"year": 2026, "month": 6, "employee_id": str(plan_employee.id)},
            headers={"Authorization": f"Bearer {await _gf_token(db, tenant.id)}"},
        )
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["employee_name"] == "Paula Plan"
        assert d["employee_number"] == "PLAN1"
        assert d["year"] == 2026 and d["month"] == 6
        assert len(d["days"]) == 30  # Juni = 30 Kalendertage

        # Mo 01.06.: zwei Segmente (Pause dazwischen), Ist 614, Soll 495, +119.
        mo = d["days"][0]
        assert mo["date"] == "2026-06-01"
        assert mo["weekday"] == "Mo"
        assert [(x["start"], x["end"]) for x in mo["segments"]] == [
            ("06:31", "12:00"),
            ("12:45", "17:30"),
        ]
        assert all(x["start_punched"] is None for x in mo["segments"])
        assert all(x["clipped_earliest_start"] is False for x in mo["segments"])
        # 45 min gestempelt reichen fuer 10:14 Arbeitszeit -> keine Anhebung.
        assert mo["pause_minutes"] == 45
        assert mo["pause_punched_minutes"] == 45
        assert mo["pause_raised_by_law"] is False
        assert mo["ist_minutes"] == 614
        assert mo["soll_minutes"] == 495
        assert mo["diff_minutes"] == 119
        assert mo["absence"] is None
        assert mo["holiday"] is False

        # Di 02.06.: 8:15 am Stueck ohne gestempelte Pause -> ArbZG §4 setzt
        # 30 min an (>6 h), Ist sinkt entsprechend. Gleiche Rechnung wie im
        # Ueberstundenkonto und im DATEV-Export.
        di = d["days"][1]
        assert di["pause_punched_minutes"] == 0
        assert di["pause_minutes"] == 30
        assert di["pause_raised_by_law"] is True
        assert di["ist_minutes"] == 465
        assert di["diff_minutes"] == -30

        # Mi 03.06.: Urlaub — kein Soll am Tag, diff 0, Label gesetzt.
        mi = d["days"][2]
        assert mi["absence"] == "Urlaub"
        assert mi["soll_minutes"] == 0
        assert mi["ist_minutes"] == 0
        assert mi["diff_minutes"] == 0

        # Wochenende: Sa 06.06. ohne Soll.
        sa = d["days"][5]
        assert sa["weekday"] == "Sa"
        assert sa["soll_minutes"] == 0

        # Summary: Juni 2026 (kein HH-Feiertag) = 18x Mo–Do + 4x Fr
        # -> Brutto-Soll 18*495 + 4*330 = 10230, minus Urlaubstag (495) = 9735.
        s = d["summary"]
        assert s["ist_minutes"] == 614 + 465
        assert s["soll_minutes"] == 10230 - 495
        assert s["saldo_minutes"] == s["ist_minutes"] - s["soll_minutes"]
        assert s["urlaub_minutes"] == 495
        assert s["urlaub_tage"] == 1.0
        assert s["krank_minutes"] == 0
        assert s["krank_tage"] == 0.0

    async def test_ohne_rechte_403(self, db, client, tenant, plan_employee) -> None:
        r = await client.get(
            "/api/v1/admin/monthly-timesheet",
            params={"year": 2026, "month": 6, "employee_id": str(plan_employee.id)},
            headers={"Authorization": f"Bearer {await _no_perm_token(db, tenant.id)}"},
        )
        assert r.status_code == 403

    async def test_unbekannter_mitarbeiter_404(self, db, client, tenant) -> None:
        r = await client.get(
            "/api/v1/admin/monthly-timesheet",
            params={"year": 2026, "month": 6, "employee_id": str(uuid.uuid4())},
            headers={"Authorization": f"Bearer {await _gf_token(db, tenant.id)}"},
        )
        assert r.status_code == 404


class TestFruehesterBeginnUndPause:
    """Zettel, Ueberstundenkonto und Export muessen dieselbe Zeit bewerten.

    Vorher galt: der Zettel kappte den fruehesten Beginn, rechnete aber ohne
    gesetzliche Mindestpause; das Konto machte es genau andersherum. Dieselbe
    Person hatte damit im selben Monat zwei Istzeiten.
    """

    async def _mit_profil(self, db, tenant, user: User) -> None:
        """Mitarbeiter einem Profil mit fruehestem Beginn 06:00 zuordnen."""
        settings = dict(tenant.settings or {})
        settings["wage_profiles"] = [
            {"id": "prof-06", "name": "Ab 06:00", "fruehester_beginn": "06:00"}
        ]
        tenant.settings = settings
        user.wage_profile_id = "prof-06"
        await db.flush()

    async def test_zettel_zeigt_gestempelt_und_gewertet(
        self, db, client, tenant, plan_employee
    ) -> None:
        await self._mit_profil(db, tenant, plan_employee)
        now = datetime.now(UTC)
        # Mo 01.06.: 05:55 gestempelt, gewertet erst ab 06:00; 8:20 Anwesenheit
        # ohne Pause -> ArbZG hebt auf 30 min an.
        for etype, t in [(EventType.PUNCH_IN, "05:55"), (EventType.PUNCH_OUT, "14:20")]:
            db.add(
                PunchEvent(
                    tenant_id=tenant.id,
                    employee_id=plan_employee.id,
                    event_type=etype,
                    event_time=_utc(1, t),
                    source=EventSource.NFC_TERMINAL,
                    created_at=now,
                )
            )
        await _recompute(db, tenant, plan_employee, 1)
        await db.commit()

        r = await client.get(
            "/api/v1/admin/monthly-timesheet",
            params={"year": 2026, "month": 6, "employee_id": str(plan_employee.id)},
            headers={"Authorization": f"Bearer {await _gf_token(db, tenant.id)}"},
        )
        assert r.status_code == 200, r.text
        mo = r.json()["days"][0]

        seg = mo["segments"][0]
        assert seg["start"] == "06:00", "gewertet wird ab dem fruehesten Beginn"
        assert seg["start_punched"] == "05:55", "der Rohstempel bleibt sichtbar"
        assert seg["clipped_earliest_start"] is True

        # 06:00–14:20 = 500 min Anwesenheit, davon 30 min Pflichtpause.
        assert mo["pause_punched_minutes"] == 0
        assert mo["pause_minutes"] == 30
        assert mo["pause_raised_by_law"] is True
        assert mo["ist_minutes"] == 470

    async def test_konto_rechnet_wie_der_zettel(self, db, client, tenant, plan_employee) -> None:
        from zeiterfassung.services.clock_service import recompute_daily_timesheet

        await self._mit_profil(db, tenant, plan_employee)
        now = datetime.now(UTC)
        for etype, t in [(EventType.PUNCH_IN, "05:55"), (EventType.PUNCH_OUT, "14:20")]:
            db.add(
                PunchEvent(
                    tenant_id=tenant.id,
                    employee_id=plan_employee.id,
                    event_type=etype,
                    event_time=_utc(1, t),
                    source=EventSource.NFC_TERMINAL,
                    created_at=now,
                )
            )
        await _recompute(db, tenant, plan_employee, 1)
        await db.commit()

        sheet = await recompute_daily_timesheet(db, tenant.id, plan_employee.id, date(2026, 6, 1))
        assert sheet.net_work_minutes == 470, "gleiche Istzeit wie im Stundenzettel"
        assert sheet.break_minutes == 30

    async def test_nfc_profil_kappt_auch_das_fensterende_im_zettel(
        self, db, client, tenant, plan_employee
    ) -> None:
        """Arbeitszeit-Profil (kind 'nfc', Spec 2026-08-14): der Zettel kappt mit
        dem Fenster — auch am ENDE — und zeigt den Rohstempel in Klammern."""
        from zeiterfassung.models.work_time_profile import WorkTimeProfile, WorkTimeProfileKind

        profil = WorkTimeProfile(
            tenant_id=tenant.id,
            name="Tagfenster 06-16",
            kind=WorkTimeProfileKind.NFC,
            is_active=True,
            clock_start="06:00",
            clock_end="16:00",
        )
        db.add(profil)
        await db.flush()
        plan_employee.work_time_profile_id = profil.id

        now = datetime.now(UTC)
        # Mo 01.06.: 06:31–16:30 gestempelt, Pause 12:00–12:45. Gewertet wird
        # nur bis 16:00: 06:31–16:00 = 9:29 minus 45 min Pause = 8:44 (524).
        for etype, t in [
            (EventType.PUNCH_IN, "06:31"),
            (EventType.BREAK_START, "12:00"),
            (EventType.BREAK_END, "12:45"),
            (EventType.PUNCH_OUT, "16:30"),
        ]:
            db.add(
                PunchEvent(
                    tenant_id=tenant.id,
                    employee_id=plan_employee.id,
                    event_type=etype,
                    event_time=_utc(1, t),
                    source=EventSource.NFC_TERMINAL,
                    created_at=now,
                )
            )
        await _recompute(db, tenant, plan_employee, 1)
        await db.commit()

        r = await client.get(
            "/api/v1/admin/monthly-timesheet",
            params={"year": 2026, "month": 6, "employee_id": str(plan_employee.id)},
            headers={"Authorization": f"Bearer {await _gf_token(db, tenant.id)}"},
        )
        assert r.status_code == 200, r.text
        mo = r.json()["days"][0]

        assert mo["ist_minutes"] == 524
        letztes = mo["segments"][-1]
        assert letztes["end"] == "16:00", "gewertet nur bis zum Fensterende"
        assert letztes["end_punched"] == "16:30", "der Rohstempel bleibt sichtbar"
        assert letztes["clipped_window_end"] is True
        # Der Beginn 06:31 liegt im Fenster -> keine Beginn-Kappung.
        assert mo["segments"][0]["start"] == "06:31"
        assert mo["segments"][0]["clipped_earliest_start"] is False


class TestGleicherZeitstempel:
    """Regression: BREAK_START/BREAK_END/PUNCH_OUT in derselben Minute.

    Am Terminal entstehen die drei Events in einer Sekunde (Pause-Taste +
    Feierabend). Ohne id als zweites Sortierkriterium war die Reihenfolge bei
    gleichem event_time undefiniert — kam PUNCH_OUT vor BREAK_END, blieb das
    letzte Segment offen und lief ueber Mitternacht (Werkstudentin, 18.08.:
    Zettel 14,92 h statt 7,78 h). Der Zettel muss exakt rechnen wie der
    Tagesbogen (clock_service sortiert seit jeher event_time, id).
    """

    async def test_pause_und_feierabend_in_derselben_minute(
        self, db, client, tenant, plan_employee
    ) -> None:
        now = datetime.now(UTC)
        # Mo 01.06. 06:20–14:38 lokal; drei Events um 14:38 in id-Reihenfolge
        # BREAK_START, BREAK_END, PUNCH_OUT — dazu der Folgetag, in den ein
        # offen gebliebenes Segment hineinlaufen wuerde.
        punches = [
            (1, EventType.PUNCH_IN, "06:20"),
            (1, EventType.BREAK_START, "12:31"),
            (1, EventType.BREAK_END, "12:39"),
            (1, EventType.BREAK_START, "14:38"),
            (1, EventType.BREAK_END, "14:38"),
            (1, EventType.PUNCH_OUT, "14:38"),
            (2, EventType.PUNCH_IN, "06:15"),
            (2, EventType.PUNCH_OUT, "14:50"),
        ]
        for day, etype, t in punches:
            db.add(
                PunchEvent(
                    tenant_id=tenant.id,
                    employee_id=plan_employee.id,
                    event_type=etype,
                    event_time=_utc(day, t),
                    source=EventSource.NFC_TERMINAL,
                    created_at=now,
                )
            )
        await _recompute(db, tenant, plan_employee, 1, 2)
        await db.commit()

        r = await client.get(
            "/api/v1/admin/monthly-timesheet",
            params={"year": 2026, "month": 6, "employee_id": str(plan_employee.id)},
            headers={"Authorization": f"Bearer {await _gf_token(db, tenant.id)}"},
        )
        assert r.status_code == 200, r.text
        d = r.json()

        # Mo: Segmente enden um 14:38, NICHTS laeuft bis Mitternacht.
        mo = d["days"][0]
        assert [(x["start"], x["end"]) for x in mo["segments"]] == [
            ("06:20", "12:31"),
            ("12:39", "14:38"),
        ]
        # 06:20–14:38 = 498 brutto, 8 min gestempelte Pause -> ArbZG hebt auf 30.
        assert mo["ist_minutes"] == 468
        assert mo["pause_minutes"] == 30
        # Di: unberuehrt vom Vortag, beginnt um 06:15 (nicht 00:00).
        di = d["days"][1]
        assert [(x["start"], x["end"]) for x in di["segments"]] == [("06:15", "14:50")]
        assert di["ist_minutes"] == 485  # 515 brutto - 30 ArbZG


class TestTagOhneStempel:
    """Regression: Tage ohne punch_events fehlten im Monatszettel.

    Auto-Erfasser und manuell gesetzte Tage haben einen Tagesbogen, aber keine
    Stempel — der Zettel zeigte dort 0,00 h (Juli: Zettel 0,00 h,
    Tagesboegen 184,00 h). Der Bogen ist dann die Quelle; Tage MIT Stempeln
    rechnen weiter aus den Events.
    """

    async def test_tagesbogen_ohne_stempel_zaehlt_im_zettel(
        self, db, client, tenant, plan_employee
    ) -> None:
        from zeiterfassung.models.clock import DailyTimesheet, TimesheetStatus

        now = datetime.now(UTC)
        # Mo 01.06.: nur Tagesbogen (Auto-Erfassung), 05:00–09:30 UTC = 240 net.
        db.add(
            DailyTimesheet(
                tenant_id=tenant.id,
                employee_id=plan_employee.id,
                work_date=date(2026, 6, 1),
                start_time=datetime(2026, 6, 1, 5, 0, tzinfo=UTC),
                end_time=datetime(2026, 6, 1, 9, 30, tzinfo=UTC),
                break_minutes=30,
                net_work_minutes=240,
                gross_work_minutes=270,
                status=TimesheetStatus.COMPLETE,
                created_at=now,
                updated_at=now,
            )
        )
        # Di 02.06.: Stempel UND (abgeleiteter) Tagesbogen — Events gewinnen,
        # nichts wird doppelt gezaehlt.
        for etype, t in [(EventType.PUNCH_IN, "06:00"), (EventType.PUNCH_OUT, "14:15")]:
            db.add(
                PunchEvent(
                    tenant_id=tenant.id,
                    employee_id=plan_employee.id,
                    event_type=etype,
                    event_time=_utc(2, t),
                    source=EventSource.NFC_TERMINAL,
                    created_at=now,
                )
            )
        db.add(
            DailyTimesheet(
                tenant_id=tenant.id,
                employee_id=plan_employee.id,
                work_date=date(2026, 6, 2),
                start_time=_utc(2, "06:00"),
                end_time=_utc(2, "14:15"),
                break_minutes=30,
                net_work_minutes=465,
                gross_work_minutes=495,
                status=TimesheetStatus.COMPLETE,
                created_at=now,
                updated_at=now,
            )
        )
        await db.commit()

        r = await client.get(
            "/api/v1/admin/monthly-timesheet",
            params={"year": 2026, "month": 6, "employee_id": str(plan_employee.id)},
            headers={"Authorization": f"Bearer {await _gf_token(db, tenant.id)}"},
        )
        assert r.status_code == 200, r.text
        d = r.json()

        # Mo: Zeit aus dem Bogen, Segment 07:00–11:30 lokal (CEST = UTC+2).
        mo = d["days"][0]
        assert mo["ist_minutes"] == 240
        assert mo["pause_minutes"] == 30
        assert [(x["start"], x["end"]) for x in mo["segments"]] == [("07:00", "11:30")]
        # Di: aus den Stempeln gerechnet (495 brutto - 30 ArbZG), NICHT der
        # Bogen-Wert plus Events.
        di = d["days"][1]
        assert di["ist_minutes"] == 465
        assert [(x["start"], x["end"]) for x in di["segments"]] == [("06:00", "14:15")]
        # Summe: genau beide Tage, nichts doppelt.
        assert d["summary"]["ist_minutes"] == 240 + 465


class TestAusstempelnVergessen:
    """Der Zettel wertet 'Ausstempeln vergessen' wie Tagesbogen und Tagesdetail.

    Ein SYSTEM_AUTO_TIMEOUT-Feierabend heisst: die echten Stunden fehlen noch.
    Der Bogen zaehlt den Tag 0 h + Flag; der Zettel zeigte bisher die volle
    Rohzeit bis zum Timeout (real: zwei Mitarbeiter mit je ~13 h Fantasie).
    """

    async def test_zettel_zeigt_0h_und_warnung(self, db, client, tenant, plan_employee) -> None:
        now = datetime.now(UTC)
        for etype, t, src in [
            (EventType.PUNCH_IN, "06:39", EventSource.NFC_TERMINAL),
            (EventType.PUNCH_OUT, "20:39", EventSource.SYSTEM_AUTO_TIMEOUT),
        ]:
            db.add(
                PunchEvent(
                    tenant_id=tenant.id,
                    employee_id=plan_employee.id,
                    event_type=etype,
                    event_time=_utc(1, t),
                    source=src,
                    created_at=now,
                )
            )
        await _recompute(db, tenant, plan_employee, 1)
        await db.commit()

        r = await client.get(
            "/api/v1/admin/monthly-timesheet",
            params={"year": 2026, "month": 6, "employee_id": str(plan_employee.id)},
            headers={"Authorization": f"Bearer {await _gf_token(db, tenant.id)}"},
        )
        assert r.status_code == 200, r.text
        mo = r.json()["days"][0]
        assert mo["ist_minutes"] == 0, "Auto-Timeout ist kein Stundenwert"
        assert mo["flagged_reason"], "die 0 braucht ihre Erklaerung auf dem Blatt"
        assert mo["segments"], "die Rohstempel bleiben sichtbar (wie im Tagesdetail)"
        assert r.json()["summary"]["ist_minutes"] == 0


class TestSekundenRundung:
    """Zettel = Bogen auf die Minute — auch bei Stempeln mit Sekunden.

    Der Bogen summiert sekundengenau und rundet am Ende; der Zettel rechnete
    je Segment in vollen Minuten und verlor pro Paar bis zu 59 Sekunden.
    Seit der Zettel das Bogen-Ist liest, ist das konstruktionsbedingt gleich.
    """

    async def test_zettel_gleich_bogen_bei_sekunden(
        self, db, client, tenant, plan_employee
    ) -> None:
        from zeiterfassung.services.clock_service import recompute_daily_timesheet

        now = datetime.now(UTC)
        # Drei Segmente mit je :50-Sekunden-Grenzen — Minutenrundung je Segment
        # wuerde ~2 min verlieren.
        ereignisse = [
            (EventType.PUNCH_IN, 6, 0, 50),
            (EventType.BREAK_START, 9, 30, 40),
            (EventType.BREAK_END, 9, 40, 30),
            (EventType.BREAK_START, 12, 0, 20),
            (EventType.BREAK_END, 12, 30, 10),
            (EventType.PUNCH_OUT, 14, 15, 50),
        ]
        for etype, h, m, sec in ereignisse:
            db.add(
                PunchEvent(
                    tenant_id=tenant.id,
                    employee_id=plan_employee.id,
                    event_type=etype,
                    event_time=datetime(2026, 6, 1, h - 2, m, sec, tzinfo=UTC),  # lokal -> UTC
                    source=EventSource.NFC_TERMINAL,
                    created_at=now,
                )
            )
        await _recompute(db, tenant, plan_employee, 1)
        await db.commit()

        bogen = await recompute_daily_timesheet(db, tenant.id, plan_employee.id, date(2026, 6, 1))
        r = await client.get(
            "/api/v1/admin/monthly-timesheet",
            params={"year": 2026, "month": 6, "employee_id": str(plan_employee.id)},
            headers={"Authorization": f"Bearer {await _gf_token(db, tenant.id)}"},
        )
        assert r.status_code == 200, r.text
        mo = r.json()["days"][0]
        assert mo["ist_minutes"] == bogen.net_work_minutes
        assert mo["pause_minutes"] == bogen.break_minutes


class TestZettelOhnePausenwarnung:
    async def test_arbzg_pausenwarnung_bleibt_vom_zettel_weg(
        self, db, client, tenant, plan_employee
    ) -> None:
        """Ab dem Stichtag der Pausen-Betriebsregel flaggt der Tagesbogen zu kurz
        gestempelte Pausen; auf dem Zettel steht das schon als "(0:00)§" —
        die rote Warnung soll dort nicht zusaetzlich erscheinen."""
        from zeiterfassung.services import clock_service

        now = datetime.now(UTC)
        # Mo 31.08.2026 (nach Stichtag 28.08.), 06:46–16:00 ohne Pause (CEST).
        for etype, hh, mm in ((EventType.PUNCH_IN, 4, 46), (EventType.PUNCH_OUT, 14, 0)):
            db.add(
                PunchEvent(
                    tenant_id=tenant.id,
                    employee_id=plan_employee.id,
                    event_type=etype,
                    event_time=datetime(2026, 8, 31, hh, mm, tzinfo=UTC),
                    source=EventSource.NFC_TERMINAL,
                    created_at=now,
                )
            )
        await clock_service.recompute_daily_timesheet(
            db, tenant.id, plan_employee.id, date(2026, 8, 31)
        )
        await db.commit()

        r = await client.get(
            "/api/v1/admin/monthly-timesheet",
            params={"year": 2026, "month": 8, "employee_id": str(plan_employee.id)},
            headers={"Authorization": f"Bearer {await _gf_token(db, tenant.id)}"},
        )
        assert r.status_code == 200, r.text
        tag = r.json()["days"][30]
        assert tag["date"] == "2026-08-31"
        assert tag["pause_raised_by_law"] is True
        assert tag["flagged_reason"] is None
