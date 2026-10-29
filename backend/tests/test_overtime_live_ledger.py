"""live_balance: eine Wahrheit aus Ledger + offenen Monaten (Issue #6).

Deckt ab: ohne Monatsabschluss identisch zur reinen Tagesrechnung; ein per
accrual abgeschlossener Monat kommt aus dem Ledger und wird NICHT neu gerechnet
(Lock — rückwirkende Tagesbogen-Änderungen ändern den Saldo nicht mehr still);
Auszahlungen (payout) reduzieren den Saldo; der Tages-Verlauf (daily_breakdown)
ersetzt abgeschlossene Monate durch ihre Ledger-Zeilen und endet exakt auf dem
Karten-Saldo.

„Heute" wird pro Test fest übergeben, damit die Ergebnisse unabhängig von der
Systemuhr sind.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from zeiterfassung.models.clock import DailyTimesheet
from zeiterfassung.models.overtime import OvertimeEntry
from zeiterfassung.services import overtime_service as ot

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _ts(tenant_id, emp_id, d, net):
    return DailyTimesheet(
        tenant_id=tenant_id,
        employee_id=emp_id,
        work_date=d,
        start_time=datetime(d.year, d.month, d.day, 8, 0, tzinfo=UTC),
        break_minutes=0,
        net_work_minutes=net,
        gross_work_minutes=net,
        overtime_minutes=0,
        is_holiday_work=False,
        created_at=NOW,
        updated_at=NOW,
    )


def _entry(tenant_id, emp_id, ym, entry_date, entry_type, hours, reason=None):
    return OvertimeEntry(
        tenant_id=tenant_id,
        employee_id=emp_id,
        year_month=ym,
        entry_date=entry_date,
        entry_type=entry_type,
        hours=Decimal(str(hours)),
        reason=reason,
        created_at=NOW,
    )


@pytest.mark.asyncio
async def test_live_balance_default_today_is_tenant_local_not_utc(
    monkeypatch, db, tenant, employee
):
    """``today=None`` (Default) muss das tenant-lokale Datum verwenden, nicht UTC (#8).

    23:20 UTC am 1.4.2026 == 01:20 CEST am 2.4.2026 (Europe/Berlin, Sommerzeit
    seit 29.3.). Mit tenant-lokalem Datum (heute = 2.4.) sind beide Tage
    Vergangenheit und zählen (2.00); beim UTC-Datum wäre der 1.4. noch "heute"
    und fiele raus, weil der laufende Tag nicht ins Konto zählt. Selbe
    Ableitung wie ``clock_service`` (#3/#140).
    """
    fixed_now_utc = datetime(2026, 4, 1, 23, 20, tzinfo=UTC)

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now_utc.astimezone(tz) if tz else fixed_now_utc

    monkeypatch.setattr(ot, "datetime", _FixedDatetime)

    t = tenant.id
    # Auto-Erfassung, damit Lücken-Tage (der Rest von März/April) keine
    # Unentschuldigt-Zeilen erzeugen — hier geht es nur um die Monatsgrenze.
    employee.time_capture = "auto"
    # Di 31.3. (Konto-Start via erstem Tagesbogen) +1h, Mi 1.4. +1h — beides
    # normale Werktage ohne Feiertag in HH.
    db.add(_ts(t, employee.id, date(2026, 3, 31), 540))
    db.add(_ts(t, employee.id, date(2026, 4, 1), 540))
    await db.flush()

    bal = await ot.live_balance(db, t, employee)  # today=None -> tenant-lokal ableiten
    assert bal == Decimal("2.00")  # März + April; bei UTC-Datum fiele der 1.4. raus (nur 1.00)


@pytest.mark.asyncio
async def test_live_balances_for_tenant_default_today_is_tenant_local_not_utc(
    monkeypatch, db, tenant, employee
):
    """Gleiche Ableitung wie oben, aber über den Mandanten-weiten Pfad (#8)."""
    fixed_now_utc = datetime(2026, 4, 1, 23, 20, tzinfo=UTC)

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now_utc.astimezone(tz) if tz else fixed_now_utc

    monkeypatch.setattr(ot, "datetime", _FixedDatetime)

    t = tenant.id
    employee.time_capture = "auto"
    db.add(_ts(t, employee.id, date(2026, 3, 31), 540))
    db.add(_ts(t, employee.id, date(2026, 4, 1), 540))
    await db.flush()

    balances = await ot.live_balances_for_tenant(db, t)
    assert balances[employee.id] == Decimal("2.00")


@pytest.mark.asyncio
async def test_live_balance_without_settlement_is_daily_sum_plus_adjustments(db, tenant, employee):
    """Kein Monat abgeschlossen → Tagesrechnung + Korrekturen (Verhalten wie bisher)."""
    t = tenant.id
    # Mi 1.7. +1h, Do 2.7. genau Soll, Fr 3.7. −1h → Tages-Netto 0.
    db.add(_ts(t, employee.id, date(2026, 7, 1), 540))
    db.add(_ts(t, employee.id, date(2026, 7, 2), 480))
    db.add(_ts(t, employee.id, date(2026, 7, 3), 420))
    db.add(_entry(t, employee.id, date(2026, 7, 1), date(2026, 7, 3), "adjustment", "2.00"))
    await db.flush()

    bal = await ot.live_balance(db, t, employee, today=date(2026, 7, 4))
    assert bal == Decimal("2.00")


@pytest.mark.asyncio
async def test_live_balance_settled_month_comes_from_ledger_not_recomputed(db, tenant, employee):
    """Abgeschlossener Monat: die accrual-Zeile zählt — nicht die Tagesbögen.

    Die Juni-Tagesbögen ergäben +2h; die (eingefrorene) Ansammlung sagt +5h.
    Der Saldo folgt dem Ledger — rückwirkende Änderungen an Tagesbögen eines
    abgeschlossenen Monats ändern den Saldo nicht mehr still.
    """
    t = tenant.id
    # Juni (abgeschlossen): Mo 1.6. +1h, Di 2.6. +1h → Tagesrechnung wäre +2h …
    db.add(_ts(t, employee.id, date(2026, 6, 1), 540))
    db.add(_ts(t, employee.id, date(2026, 6, 2), 540))
    # … aber die accrual-Zeile (settle_month) hat +5h festgeschrieben.
    db.add(
        _entry(
            t,
            employee.id,
            date(2026, 6, 1),
            date(2026, 6, 30),
            "accrual",
            "5.00",
            reason="Monatsansammlung (netto)",
        )
    )
    # Juli (offen): Mi 1.7. +1h → live.
    db.add(_ts(t, employee.id, date(2026, 7, 1), 540))
    await db.flush()

    bal = await ot.live_balance(db, t, employee, today=date(2026, 7, 2))
    assert bal == Decimal("6.00")  # 5 (Ledger) + 1 (Juli live); NICHT 2+1 oder 5+2+1


@pytest.mark.asyncio
async def test_live_balance_payout_reduces_balance(db, tenant, employee):
    """Auszahlungen verlassen das Konto: accrual +10 − payout 4 = 6."""
    t = tenant.id
    db.add(
        _entry(
            t,
            employee.id,
            date(2026, 6, 1),
            date(2026, 6, 30),
            "accrual",
            "10.00",
            reason="Monatsansammlung (netto)",
        )
    )
    db.add(
        _entry(
            t,
            employee.id,
            date(2026, 6, 1),
            date(2026, 6, 30),
            "payout",
            "-4.00",
            reason="Auszahlung (Modus above_cap)",
        )
    )
    await db.flush()

    bal = await ot.live_balance(db, t, employee, today=date(2026, 7, 2))
    assert bal == Decimal("6.00")


@pytest.mark.asyncio
async def test_daily_breakdown_stays_day_granular_and_ends_at_live_balance(db, tenant, employee):
    """Verlauf bleibt tagesgenau — auch für abgeschlossene Monate.

    Die Juni-Tage stehen einzeln im Verlauf (keine accrual-Zeile); die
    Auszahlung erscheint als eigene Zeile. Solange der abgeschlossene Monat
    nicht rückwirkend verändert wurde (accrual == Tagessumme), endet der
    Verlauf exakt auf dem Karten-Saldo (live_balance).
    """
    t = tenant.id
    # Auto-Erfassung, damit die Lücken-Tage des Junis keine Unentschuldigt-
    # Zeilen erzeugen (die NFC-Regel deckt test_overtime_daily ab).
    employee.time_capture = "auto"
    # Juni (abgeschlossen): Mo 1.6. +1h, Di 2.6. +1h; accrual == Tagessumme (+2h)
    # wie von settle_month gebucht, dazu eine Auszahlung von 1h.
    db.add(_ts(t, employee.id, date(2026, 6, 1), 540))
    db.add(_ts(t, employee.id, date(2026, 6, 2), 540))
    db.add(
        _entry(
            t,
            employee.id,
            date(2026, 6, 1),
            date(2026, 6, 30),
            "accrual",
            "2.00",
            reason="Monatsansammlung (netto)",
        )
    )
    db.add(
        _entry(
            t,
            employee.id,
            date(2026, 6, 1),
            date(2026, 6, 30),
            "payout",
            "-1.00",
            reason="Auszahlung (Modus above_cap)",
        )
    )
    db.add(_ts(t, employee.id, date(2026, 7, 1), 540))  # Juli offen: +1h
    db.add(_entry(t, employee.id, date(2026, 7, 1), date(2026, 7, 1), "adjustment", "2.00"))
    await db.flush()

    today = date(2026, 7, 2)
    rows = await ot.daily_breakdown(db, t, employee, today=today)

    # Juni-Tage einzeln sichtbar, KEINE accrual-Zeile, Auszahlung als Zeile.
    june_days = [r for r in rows if r["type"] == "day" and r["date"].startswith("2026-06")]
    assert len(june_days) == 2
    assert not any(r["type"] == "accrual" for r in rows)
    payouts = [r for r in rows if r["type"] == "payout"]
    assert len(payouts) == 1
    assert payouts[0]["delta_hours"] == -1.0

    # Laufender Saldo endet exakt auf dem Karten-Saldo (live_balance):
    # 2 (Juni, accrual == Tagessumme) − 1 (payout) + 1 (Juli) + 2 (Korrektur) = 4.
    bal = await ot.live_balance(db, t, employee, today=today)
    assert rows, "Verlauf darf nicht leer sein"
    assert Decimal(str(rows[-1]["balance_hours"])) == bal == Decimal("4.00")
