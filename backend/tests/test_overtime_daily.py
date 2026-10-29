"""Überstunden-Saldo tagesgenau.

Deckt ab: kein unechtes Minus durch Urlaub/Krank/leere Bögen; Halbtags-Urlaub;
echtes Minus bei Unterschreitung; Tages-Verlauf mit laufendem Saldo; und das
unentschuldigte Fehlen von NFC-Stemplern (vergangener Werktag ohne Stempel/Abwesenheit
→ −Tagessoll), das für web/auto-Erfasste nicht greift.

„Heute" wird pro Test fest übergeben, damit die Ergebnisse unabhängig von der Systemuhr sind.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from zeiterfassung.models.absence import (
    ApprovalRequest,
    DayPart,
    RequestStatus,
    RequestType,
)
from zeiterfassung.models.clock import DailyTimesheet
from zeiterfassung.services import overtime_service as ot

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _ts(tenant_id, emp_id, d, net, *, start=True, holiday=False):
    return DailyTimesheet(
        tenant_id=tenant_id,
        employee_id=emp_id,
        work_date=d,
        start_time=datetime(d.year, d.month, d.day, 8, 0, tzinfo=UTC) if start else None,
        break_minutes=0,
        net_work_minutes=net,
        gross_work_minutes=net,
        overtime_minutes=0,
        is_holiday_work=holiday,
        created_at=NOW,
        updated_at=NOW,
    )


def _absence(tenant_id, emp_id, kind, d_from, d_to, days, part=DayPart.FULL):
    return ApprovalRequest(
        tenant_id=tenant_id,
        employee_id=emp_id,
        request_type=kind,
        status=RequestStatus.APPROVED,
        date_from=d_from,
        date_to=d_to,
        day_part=part,
        days_count=Decimal(str(days)),
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.mark.asyncio
async def test_absence_and_empty_days_do_not_create_minus(db, tenant, employee):
    """Juli: Mi/Do je 8h30 (+30 min) = +60 min. Sa-Arbeit zählt NICHT ins Konto
    (eigene Lohnart/Zuschläge, #21). Krank (Fr) + Urlaub (Mo–Fr) dürfen nichts abziehen."""
    t, e = tenant.id, employee.id
    employee.hire_date = date(2026, 7, 1)
    db.add_all(
        [
            _ts(t, e, date(2026, 7, 1), 510),  # Mi 8h30 -> +30
            _ts(t, e, date(2026, 7, 2), 510),  # Do 8h30 -> +30
            _ts(t, e, date(2026, 7, 4), 390),  # Sa 6h30 -> Δ 0 (WE, eigene Lohnart)
            _ts(t, e, date(2026, 7, 3), 0, start=False),  # leerer Bogen: Krank
            *[_ts(t, e, date(2026, 7, d), 0, start=False) for d in range(13, 18)],  # leer: Urlaub
        ]
    )
    db.add_all(
        [
            _absence(t, e, RequestType.SICK_LEAVE, date(2026, 7, 3), date(2026, 7, 3), 1),
            _absence(t, e, RequestType.VACATION, date(2026, 7, 13), date(2026, 7, 17), 5),
        ]
    )
    await db.flush()

    net = await ot._net_overtime_minutes_for_month(db, t, employee, 2026, 7, today=date(2026, 7, 5))
    assert net == 60, f"erwartet +60 min (Mi/Do; Sa nicht ins Konto), war {net}"


@pytest.mark.asyncio
async def test_half_day_vacation_covers_half_the_soll(db, tenant, employee):
    """Halber Urlaubstag + 4h gearbeitet: Soll für den Tag = 4h, Ist = 4h → 0, kein Minus."""
    t, e = tenant.id, employee.id
    employee.hire_date = date(2026, 7, 6)
    db.add(_ts(t, e, date(2026, 7, 6), 240))  # Mo 4h gearbeitet
    db.add(
        _absence(
            t, e, RequestType.VACATION, date(2026, 7, 6), date(2026, 7, 6), 0.5, part=DayPart.AM
        )
    )
    await db.flush()

    net = await ot._net_overtime_minutes_for_month(db, t, employee, 2026, 7, today=date(2026, 7, 7))
    assert net == 0, f"halber Tag Urlaub + 4h Arbeit muss 0 ergeben, war {net}"


@pytest.mark.asyncio
async def test_real_undertime_still_counts_as_minus(db, tenant, employee):
    """Echtes Minus bleibt: an einem normalen Werktag nur 6h gearbeitet → −2 h (−120 min)."""
    t, e = tenant.id, employee.id
    employee.hire_date = date(2026, 7, 6)
    db.add(_ts(t, e, date(2026, 7, 6), 360))  # Mo 6h, kein Urlaub
    await db.flush()

    net = await ot._net_overtime_minutes_for_month(db, t, employee, 2026, 7, today=date(2026, 7, 7))
    assert net == -120, f"6h an Werktag = −120 min, war {net}"


@pytest.mark.asyncio
async def test_daily_breakdown_lists_each_day_with_running_balance(db, tenant, employee):
    """Der Tages-Verlauf listet jeden gearbeiteten Tag mit Δ und laufendem Saldo; das
    Endsaldo entspricht der Summe der Tageszeilen (= Karten-Saldo ohne Korrekturen)."""
    t, e = tenant.id, employee.id
    employee.hire_date = date(2026, 7, 1)
    db.add_all(
        [
            _ts(t, e, date(2026, 7, 1), 510),  # +0,5 h
            _ts(t, e, date(2026, 7, 2), 510),  # +0,5 h
            _ts(t, e, date(2026, 7, 4), 390),  # Sa: Δ 0 (eigene Lohnart, #21)
            _ts(t, e, date(2026, 7, 3), 0, start=False),
            *[_ts(t, e, date(2026, 7, d), 0, start=False) for d in range(13, 18)],
        ]
    )
    db.add_all(
        [
            _absence(t, e, RequestType.SICK_LEAVE, date(2026, 7, 3), date(2026, 7, 3), 1),
            _absence(t, e, RequestType.VACATION, date(2026, 7, 13), date(2026, 7, 17), 5),
        ]
    )
    await db.flush()

    rows = await ot.daily_breakdown(db, t, employee, today=date(2026, 7, 5))
    days = [r for r in rows if r["type"] == "day"]
    # Sa erscheint als Arbeitstag (Ist sichtbar), trägt aber 0 zum Saldo bei.
    assert [r["date"] for r in days] == ["2026-07-01", "2026-07-02", "2026-07-04"]
    assert [r["delta_hours"] for r in days] == [0.5, 0.5, 0.0]
    assert days[-1]["balance_hours"] == 1.0  # laufender Saldo am Ende (nur Mi/Do)
    assert all(r["date"] not in ("2026-07-03", "2026-07-13") for r in rows)


@pytest.mark.asyncio
async def test_daily_breakdown_balance_is_minute_exact(db, tenant, employee):
    """Minuten-genau statt 0,01-h-Schritte: +25 min, +25 min, −5 min → Saldo 45 min.

    Vorher rundete jede Zeile auf 2 Dezimalstunden (25 min → 0,42 h = 25,2 min),
    der laufende Saldo summierte die Rundungsfehler: 0,42+0,42−0,08 = 0,76 h,
    in der Anzeige 46 min statt 45."""
    t, e = tenant.id, employee.id
    employee.hire_date = date(2026, 7, 1)
    db.add_all(
        [
            _ts(t, e, date(2026, 7, 1), 505),  # Mi +25 min
            _ts(t, e, date(2026, 7, 2), 505),  # Do +25 min
            _ts(t, e, date(2026, 7, 3), 475),  # Fr −5 min
        ]
    )
    await db.flush()

    rows = await ot.daily_breakdown(db, t, employee, today=date(2026, 7, 4))
    days = [r for r in rows if r["type"] == "day"]
    assert [round(r["delta_hours"] * 60) for r in days] == [25, 25, -5]
    balances_min = [round(r["balance_hours"] * 60) for r in days]
    assert balances_min == [25, 50, 45], f"Saldo-Verlauf in Minuten, war {balances_min}"


@pytest.mark.asyncio
async def test_unexcused_absence_of_nfc_stamper_counts_as_minus(db, tenant, employee):
    """NFC-Stempler: vergangener Werktag ohne Stempel und ohne Abwesenheit → −8 h."""
    t, _e = tenant.id, employee.id
    employee.time_capture = "nfc"
    employee.hire_date = date(2026, 7, 2)  # ab hier zählt
    await db.flush()

    # Kein Tagesbogen, keine Abwesenheit am Do 02.07.; heute = Fr 03.07. → 02.07. ist vorbei.
    net = await ot._net_overtime_minutes_for_month(db, t, employee, 2026, 7, today=date(2026, 7, 3))
    assert net == -480, f"unentschuldigtes Fehlen = −480 min, war {net}"

    rows = await ot.daily_breakdown(db, t, employee, today=date(2026, 7, 3))
    absent = [r for r in rows if r.get("absent")]
    assert len(absent) == 1 and absent[0]["date"] == "2026-07-02"
    assert absent[0]["delta_hours"] == -8.0


@pytest.mark.asyncio
async def test_nothing_counts_before_hire_date(db, tenant, employee):
    """Ab Stichtag: gebuchte Zeit VOR dem Eintrittsdatum zählt nicht (nur ab hire_date)."""
    t, e = tenant.id, employee.id
    employee.time_capture = "nfc"
    employee.hire_date = date(2026, 7, 15)  # Eintritt Mitte Juli
    db.add_all(
        [
            _ts(t, e, date(2026, 7, 10), 600),  # Fr vor Eintritt, 10h — darf NICHT zählen
            _ts(t, e, date(2026, 7, 15), 600),  # Mi, Eintrittstag, 10h -> +2h
        ]
    )
    await db.flush()

    net = await ot._net_overtime_minutes_for_month(
        db, t, employee, 2026, 7, today=date(2026, 7, 16)
    )
    assert net == 120, f"nur der Eintrittstag zählt (+120 min), war {net}"

    rows = await ot.daily_breakdown(db, t, employee, today=date(2026, 7, 16))
    assert all(r["date"] != "2026-07-10" for r in rows), "Tag vor Eintritt darf nicht auftauchen"


@pytest.mark.asyncio
async def test_unexcused_appears_for_web_too_not_auto(db, tenant, employee):
    """Unentschuldigtes Fehlen entsteht automatisch bei fehlender Zeit — auch für web.
    Auto-Erfassung ist immer gefüllt und bleibt davon unberührt."""
    t, _e = tenant.id, employee.id
    employee.counting_start_date = date(2026, 7, 2)

    employee.time_capture = "web"
    await db.flush()
    web = await ot._net_overtime_minutes_for_month(db, t, employee, 2026, 7, today=date(2026, 7, 3))
    assert web == -480, f"web ohne Zeit am Werktag = −480 min, war {web}"

    employee.time_capture = "auto"
    await db.flush()
    auto = await ot._net_overtime_minutes_for_month(
        db, t, employee, 2026, 7, today=date(2026, 7, 3)
    )
    assert auto == 0, f"auto ist immer gefuellt -> kein Minus, war {auto}"


@pytest.mark.asyncio
async def test_counting_start_date_overrides_hire_date(db, tenant, employee):
    """Erfassungsbeginn (nicht Firmeneintritt) ist der Stichtag: vor dem Erfassungs-
    beginn liegende Firmen-Tage zaehlen nicht, auch wenn nach dem Eintrittsdatum."""
    t, e = tenant.id, employee.id
    employee.time_capture = "web"
    employee.hire_date = date(2026, 6, 1)  # echter Firmeneintritt
    employee.counting_start_date = date(2026, 7, 2)  # Programm zaehlt erst ab 02.07.
    db.add_all(
        [
            _ts(t, e, date(2026, 6, 15), 600),  # 10h im Juni -> darf NICHT zaehlen
            _ts(t, e, date(2026, 7, 2), 600),  # 10h am Eintritts-/Erfassungstag -> +2h
        ]
    )
    await db.flush()

    # today = 03.07.: der 02.07. ist Vergangenheit — der LAUFENDE Tag
    # zaehlt seit 2026-08-19 nicht mehr ins Konto (erst am Folgetag).
    june = await ot._net_overtime_minutes_for_month(
        db, t, employee, 2026, 6, today=date(2026, 7, 3)
    )
    july = await ot._net_overtime_minutes_for_month(
        db, t, employee, 2026, 7, today=date(2026, 7, 3)
    )
    assert june == 0, f"Juni (vor Erfassungsbeginn) darf nicht zaehlen, war {june}"
    assert july == 120, f"Juli 10h-Tag = +120 min, war {july}"

    rows = await ot.daily_breakdown(db, t, employee, today=date(2026, 7, 3))
    assert all(not r["date"].startswith("2026-06") for r in rows), "kein Juni-Tag im Verlauf"


@pytest.mark.asyncio
async def test_future_and_weekend_gaps_are_not_unexcused(db, tenant, employee):
    """Zukünftige Tage und Wochenenden zählen nie als unentschuldigtes Fehlen."""
    t, _e = tenant.id, employee.id
    employee.time_capture = "nfc"
    employee.hire_date = date(2026, 7, 1)
    await db.flush()

    # heute = Do 02.07. → nur 01.07. ist vergangener Werktag (unexcused).
    # 04./05. = WE, Rest Zukunft.
    net = await ot._net_overtime_minutes_for_month(db, t, employee, 2026, 7, today=date(2026, 7, 2))
    assert net == -480, f"nur der eine vergangene Werktag zählt, war {net}"


@pytest.mark.asyncio
async def test_weekday_specific_soll_friday_short_day(db, tenant, employee):
    """Wochentagsspezifisches Soll (Mo–Do 8:15, Fr 5:30) — Fr-Überstunden korrekt.

    Wie Screenshot: an einem kurzen Freitag zählt Ist gegen 5:30, nicht gegen 8:00.
    """
    t, e = tenant.id, employee.id
    employee.hire_date = date(2026, 7, 1)
    employee.time_capture = "auto"  # leere Werktage erzeugen kein Unentschuldigt
    employee.weekday_target_minutes = [495, 495, 495, 495, 330, 0, 0]
    # 01.07. Mi 8:15 (495) → 0; 03.07. Fr 8:44 (524) → +3:14 (gegen 5:30).
    db.add_all(
        [
            _ts(t, e, date(2026, 7, 1), 495),  # Mi genau Soll → 0
            _ts(t, e, date(2026, 7, 3), 524),  # Fr 8h44 → +194 gegen 5:30
        ]
    )
    await db.flush()

    net = await ot._net_overtime_minutes_for_month(db, t, employee, 2026, 7, today=date(2026, 7, 6))
    assert net == 194, f"Mi 0 + Fr +3:14 = +194, war {net}"

    # Gegenprobe ohne Plan: Fr zählt gegen 8:00 → 524−480 = +44; Mi 495−480 = +15.
    employee.weekday_target_minutes = None
    await db.flush()
    net_uniform = await ot._net_overtime_minutes_for_month(
        db, t, employee, 2026, 7, today=date(2026, 7, 6)
    )
    assert net_uniform == 59, f"uniform: Mi +15 + Fr +44 = +59, war {net_uniform}"


@pytest.mark.asyncio
async def test_weekday_soll_unexcused_friday_is_short(db, tenant, employee):
    """Unentschuldigter Freitag zählt nur −5:30 (Soll des Wochentags)."""
    t, _e = tenant.id, employee.id
    employee.time_capture = "nfc"
    employee.hire_date = date(2026, 7, 3)
    employee.weekday_target_minutes = [495, 495, 495, 495, 330, 0, 0]
    await db.flush()
    # heute = Sa 04.07. → nur Fr 03.07. ist vergangener Werktag, unexcused → −5:30.
    net = await ot._net_overtime_minutes_for_month(db, t, employee, 2026, 7, today=date(2026, 7, 4))
    assert net == -330, f"unentschuldigter Fr = −5:30 = −330, war {net}"


@pytest.mark.asyncio
async def test_weekend_booking_gleitzeit_counts_into_account(db, tenant, employee):
    """Wochenendarbeit: Default = Überstunden (Δ 0, Direktvergütung); mit
    Tages-Wahl weekend_booking='gleitzeit' zählt sie normal ins Konto."""
    t, e = tenant.id, employee.id
    employee.hire_date = date(2026, 7, 1)
    sa = DailyTimesheet(
        tenant_id=t,
        employee_id=e,
        work_date=date(2026, 7, 4),  # Samstag
        start_time=datetime(2026, 7, 4, 8, 0, tzinfo=UTC),
        break_minutes=0,
        net_work_minutes=270,
        gross_work_minutes=270,
        overtime_minutes=0,
        created_at=NOW,
        updated_at=NOW,
    )
    db.add(sa)
    await db.flush()

    rows = await ot.daily_breakdown(db, t, employee, today=date(2026, 7, 6))
    day = next(r for r in rows if r["date"] == "2026-07-04")
    assert day["delta_hours"] == 0.0, "Default: Sa-Arbeit zählt nicht ins Konto"
    assert day["weekend_booking"] is None

    sa.weekend_booking = "gleitzeit"
    await db.flush()
    rows = await ot.daily_breakdown(db, t, employee, today=date(2026, 7, 6))
    day = next(r for r in rows if r["date"] == "2026-07-04")
    assert day["delta_hours"] == 4.5, "gleitzeit: Sa-Arbeit erhöht den Saldo"
    assert day["weekend_booking"] == "gleitzeit"
