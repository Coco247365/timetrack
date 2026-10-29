"""Paritaets-Tests: Konto, Stundenzettel und DATEV-Export rechnen dasselbe Soll.

Die Tagesregeln (Soll-Kette inkl. Arbeitszeit-Profil, Abwesenheits-Deckung)
liegen seit Spec 2026-08-25 in EINER Klasse (policy.tagesregeln.Tagesregeln).
Diese Tests pruefen die drei Konsumenten GEGENEINANDER — nicht gegen
Konstanten: weicht kuenftig irgendeine Stelle ab, werden sie rot. Genau die
zwei Szenarien, die vor der Konsolidierung still falsch gewesen waeren:

1. Arbeitszeit-Profil, dessen Freitag vom Wochentagsplan abweicht — Zettel
   und Export kannten das Profil nicht und haetten den Plan genommen.
2. Einzeltags-Krank mit echtem Anteil (days_count=0,75, z. B. 6 von 8 h) —
   Zettel und Export kannten nur halb/ganz und haetten 1,0 gedeckt.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from zeiterfassung.core.security import hash_password
from zeiterfassung.models.absence import ApprovalRequest, RequestStatus, RequestType
from zeiterfassung.models.clock import EventSource, EventType, PunchEvent
from zeiterfassung.models.export import WageTypeMapping
from zeiterfassung.models.user import User
from zeiterfassung.models.work_time_profile import WorkTimeProfile, WorkTimeProfileKind
from zeiterfassung.policy.context import DayPart
from zeiterfassung.services import clock_service, report_service
from zeiterfassung.services import overtime_service as ot
from zeiterfassung.services.export_service import _build_datev_rows

WAGE_TYPES = {"NORMALE_ARBEITSZEIT": 11, "MINUSSTUNDEN": 13, "KRANK_STD": 153}


async def _mk_user(db, tenant, nr: str) -> User:
    now = datetime.now(tz=UTC)
    u = User(
        tenant_id=tenant.id,
        email=f"parity-{nr}@test.de",
        hashed_password=hash_password("Passw0rd!"),
        first_name="Pari",
        last_name=f"Taet{nr}",
        employee_number=nr,
        contracted_hours_per_week=40.0,
        weekday_target_minutes=[480, 480, 480, 480, 480, 0, 0],
        employment_type="hourly",
        time_capture="nfc",
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    db.add(u)
    await db.flush()
    return u


def _soll_setup(tenant) -> None:
    tenant.settings = {**(tenant.settings or {}), "zuschlaege": {"export_basis": "soll"}}


async def _wage_types(db, tenant) -> None:
    now = datetime.now(tz=UTC)
    for code, nr in WAGE_TYPES.items():
        db.add(
            WageTypeMapping(
                tenant_id=tenant.id,
                internal_code=code,
                datev_lohnart=nr,
                description=code,
                is_active=True,
                unit="hours",
                created_at=now,
            )
        )
    await db.flush()


async def _zettel(db, tenant, user):
    return await report_service.monthly_timesheet(
        db,
        tenant.id,
        2026,
        6,
        user.id,
        has_view_all=True,
        has_view_team=False,
        caller_department_id=None,
        caller_department_ids=None,
    )


class TestProfilFreitagParitaet:
    """Auto-Profil Fr=300 min, Wochentagsplan sagt 480 — das Profil gewinnt ueberall."""

    async def test_konto_zettel_export_gleiches_soll(self, db, tenant) -> None:
        _soll_setup(tenant)
        await _wage_types(db, tenant)
        user = await _mk_user(db, tenant, "PAR1")
        tag = {"start": "07:00", "end": "15:45", "break_start": "12:00", "break_end": "12:45"}
        now = datetime.now(tz=UTC)
        profil = WorkTimeProfile(
            tenant_id=tenant.id,
            name="Buero, Freitag kurz",
            kind=WorkTimeProfileKind.AUTO,
            is_active=True,
            weekday_times={
                "0": tag,
                "1": tag,
                "2": tag,
                "3": tag,
                "4": {"start": "07:00", "end": "12:00"},  # 300 min, KEIN Plan-480
            },
            created_at=now,
            updated_at=now,
        )
        db.add(profil)
        await db.flush()
        user.work_time_profile_id = profil.id

        # Freitag 05.06.2026 gearbeitet (Stempel + Bogen wie in der Produktion).
        for etype, hh in ((EventType.PUNCH_IN, 5), (EventType.PUNCH_OUT, 11)):
            db.add(
                PunchEvent(
                    tenant_id=tenant.id,
                    employee_id=user.id,
                    event_type=etype,
                    event_time=datetime(2026, 6, 5, hh, 0, tzinfo=UTC),
                    source=EventSource.NFC_TERMINAL,
                    created_at=now,
                )
            )
        await db.flush()
        await clock_service.recompute_daily_timesheet(db, tenant.id, user.id, date(2026, 6, 5))
        await db.commit()

        # 1) Zettel
        sheet = await _zettel(db, tenant, user)
        fr = next(d for d in sheet.days if d.date == date(2026, 6, 5))
        mo = next(d for d in sheet.days if d.date == date(2026, 6, 1))

        # 2) Konto
        rows = await ot._daily_rows_for_month(db, tenant.id, user, 2026, 6, today=date(2026, 7, 15))
        konto_fr = next(r for r in rows if r["work_date"] == date(2026, 6, 5))

        # 3) Export (Soll-Modus): 011 = Summe der Tages-Solls des Monats
        datev = await _build_datev_rows(db, tenant.id, 2026, 6, [user.id], WAGE_TYPES)
        la011 = round(sum(r.wert for r in datev if r.lohnart == 11), 2)

        # Parität — gegeneinander, nicht gegen Konstanten:
        assert fr.soll_minutes == konto_fr["target_minutes"]
        assert la011 == round(sheet.summary.soll_minutes / 60, 2)
        # ... und der fachliche Kern: der Profil-Freitag ist NICHT der Plan-Wert.
        assert fr.soll_minutes == 300
        assert mo.soll_minutes == 480


class TestTeiltagsDeckungParitaet:
    """Einzeltag krank mit days_count=0,75 — alle drei decken 75 %, nicht 100 %."""

    async def test_konto_zettel_export_gleiche_deckung(self, db, tenant) -> None:
        _soll_setup(tenant)
        await _wage_types(db, tenant)
        user = await _mk_user(db, tenant, "PAR2")
        now = datetime.now(tz=UTC)
        db.add(
            ApprovalRequest(
                tenant_id=tenant.id,
                employee_id=user.id,
                request_type=RequestType.SICK_LEAVE,
                status=RequestStatus.APPROVED,
                date_from=date(2026, 6, 2),
                date_to=date(2026, 6, 2),
                day_part=DayPart.FULL,
                days_count=0.75,
                created_at=now,
                updated_at=now,
            )
        )
        await db.commit()

        sheet = await _zettel(db, tenant, user)
        di = next(d for d in sheet.days if d.date == date(2026, 6, 2))

        rows = await ot._daily_rows_for_month(db, tenant.id, user, 2026, 6, today=date(2026, 7, 15))
        konto_di = next(r for r in rows if r["work_date"] == date(2026, 6, 2))

        datev = await _build_datev_rows(db, tenant.id, 2026, 6, [user.id], WAGE_TYPES)
        krank_h = round(sum(r.wert for r in datev if r.lohnart == 153), 2)

        # Parität: Rest-Soll identisch, Krank-Stunden identisch.
        assert di.soll_minutes == konto_di["target_minutes"]
        assert round(sheet.summary.krank_minutes / 60, 2) == krank_h
        # Fachlicher Kern: 75 % gedeckt -> Rest-Soll 120, Krank 6,0 h.
        assert di.soll_minutes == 120
        assert krank_h == 6.0
