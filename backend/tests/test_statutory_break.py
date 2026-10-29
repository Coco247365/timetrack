"""Gesetzliche Pausenzeit (ArbZG §4) wird automatisch angewandt — NETTO-basiert.

Regel (Policy, Domäne D — Gesetzlich): §4 bemisst sich an der ARBEITSZEIT
(Anwesenheit minus Pausen): >6 h Arbeitszeit → 30 min, >9 h → 45 min.
- Stempelt jemand keine/zu kurze Pausen, wird nur so weit angehoben, bis die
  resultierende Arbeitszeit die Schwellen einhält (kleinste konforme Pause).
- Reicht die gestempelte Pause bereits (z. B. 9:12 Anwesenheit mit 30 min →
  8:42 Arbeitszeit), bleibt sie exakt wie gestempelt.

Die Roh-Events bleiben unangetastet (append-only) — nur der abgeleitete
Tagesbogen (break_minutes/net_work_minutes) wendet die Policy an.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from zeiterfassung.models.clock import EventSource, EventType, TimesheetStatus
from zeiterfassung.policy.statutory import effective_break_minutes
from zeiterfassung.services.clock_service import recompute_daily_timesheet, record_punch

_BERLIN = ZoneInfo("Europe/Berlin")


def test_effective_break_minutes_pure_rule():
    """§4 bemisst sich an der ARBEITSZEIT (netto), nicht an der Anwesenheit."""
    # <=6 h Anwesenheit: keine Pflichtpause; gestempelte Pausen bleiben.
    assert effective_break_minutes(360, 0) == 0
    assert effective_break_minutes(300, 15) == 15
    # Arbeitszeit > 6 h ohne (genug) Pause: auf 30 anheben.
    assert effective_break_minutes(361, 0) == 30
    assert effective_break_minutes(480, 15) == 30
    assert effective_break_minutes(480, 30) == 30
    # ausreichend gestempelt: unverändert (auch über dem Minimum).
    assert effective_break_minutes(480, 60) == 60
    # Grenzband: Anwesenheit über der Schwelle, aber Arbeitszeit nach
    # gestempelter Pause darunter -> KEINE Anhebung (vorher fälschlich brutto).
    assert effective_break_minutes(370, 15) == 15  # 6:10 anwesend -> 5:55 Arbeit
    assert effective_break_minutes(552, 30) == 30  # 9:12 anwesend -> 8:42 Arbeit
    # Ohne Stempel reicht die 30er-Pause bis 9:30 Anwesenheit (Arbeitszeit 9:00).
    assert effective_break_minutes(541, 0) == 30
    assert effective_break_minutes(570, 0) == 30
    # Arbeitszeit läge auch mit 30 min über 9 h -> 45.
    assert effective_break_minutes(571, 0) == 45
    assert effective_break_minutes(600, 30) == 45  # 10 h anwesend -> 9:30 Arbeit
    assert effective_break_minutes(600, 50) == 50


def _dt(h, m=0):
    # Berliner Ortszeit angeben, aber als UTC-Instant speichern (wie überall
    # im Code/Storage) — nur die Bucketing-/Ableitungslogik rechnet
    # UTC -> Tenant-Lokalzeit, nicht die Event-Erzeugung. Sonst würden
    # 0:00/23:59-Stempel (#3) auf den falschen Kalendertag fallen,
    # obwohl der Test einen einzelnen Berliner Tag meint.
    return datetime(2025, 3, 10, h, m, tzinfo=_BERLIN).astimezone(UTC)


async def _punch(db, tenant, employee, seq):
    for et, t in seq:
        await record_punch(
            db=db,
            tenant_id=tenant.id,
            employee_id=employee.id,
            event_type=et,
            source=EventSource.NFC_TERMINAL,
            event_time=t,
        )
    return await recompute_daily_timesheet(db, tenant.id, employee.id, date(2025, 3, 10))


async def test_no_breaks_punched_gets_statutory_break(db, tenant, employee):
    """8 h ohne Pausenstempel → automatisch 30 min Pause, kein ArbZG-Flag mehr."""
    ts = await _punch(
        db,
        tenant,
        employee,
        [(EventType.PUNCH_IN, _dt(8, 0)), (EventType.PUNCH_OUT, _dt(16, 0))],
    )
    assert ts.break_minutes == 30
    assert ts.net_work_minutes == 450  # 8 h − 30 min
    assert ts.gross_work_minutes == 480
    assert ts.status == TimesheetStatus.COMPLETE, ts.flagged_reason


async def test_no_breaks_over_9h_gets_45_minutes(db, tenant, employee):
    """>9 h ohne Pausenstempel → automatisch 45 min."""
    ts = await _punch(
        db,
        tenant,
        employee,
        [(EventType.PUNCH_IN, _dt(7, 0)), (EventType.PUNCH_OUT, _dt(17, 0))],
    )
    assert ts.break_minutes == 45
    assert ts.net_work_minutes == 555  # 10 h − 45 min


async def test_short_breaks_are_raised_to_statutory(db, tenant, employee):
    """Gestempelte 15 min bei 8,5 h Anwesenheit → auf 30 min angehoben."""
    ts = await _punch(
        db,
        tenant,
        employee,
        [
            (EventType.PUNCH_IN, _dt(8, 0)),
            (EventType.BREAK_START, _dt(12, 0)),
            (EventType.BREAK_END, _dt(12, 15)),
            (EventType.PUNCH_OUT, _dt(16, 30)),
        ],
    )
    assert ts.break_minutes == 30
    assert ts.net_work_minutes == 480  # 8,5 h − 30 min
    assert ts.status == TimesheetStatus.COMPLETE, ts.flagged_reason


async def test_sufficient_breaks_stay_untouched(db, tenant, employee):
    """Gestempelte 45 min bei 8:45 Anwesenheit → bleibt exakt wie gestempelt."""
    ts = await _punch(
        db,
        tenant,
        employee,
        [
            (EventType.PUNCH_IN, _dt(8, 0)),
            (EventType.BREAK_START, _dt(12, 0)),
            (EventType.BREAK_END, _dt(12, 45)),
            (EventType.PUNCH_OUT, _dt(16, 45)),
        ],
    )
    assert ts.break_minutes == 45
    assert ts.net_work_minutes == 480
    assert ts.status == TimesheetStatus.COMPLETE, ts.flagged_reason


async def test_borderline_band_keeps_punched_break(db, tenant, employee):
    """9:12 Anwesenheit mit 30 min gestempelt → Arbeitszeit 8:42 ≤ 9 h → 30 bleibt.

    Realfall (Altsystem-Vergleich): brutto liegt über 9 h, aber die Arbeitszeit
    nach der gestempelten Pause nicht — §4 verlangt dann KEINE 45 min."""
    ts = await _punch(
        db,
        tenant,
        employee,
        [
            (EventType.PUNCH_IN, _dt(6, 42)),
            (EventType.BREAK_START, _dt(12, 0)),
            (EventType.BREAK_END, _dt(12, 30)),
            (EventType.PUNCH_OUT, _dt(15, 54)),
        ],
    )
    assert ts.break_minutes == 30
    assert ts.net_work_minutes == 522  # 8:42 — exakt wie gestempelt
    assert ts.status == TimesheetStatus.COMPLETE, ts.flagged_reason


async def test_short_break_in_borderline_band_stays_if_net_legal(db, tenant, employee):
    """6:10 Anwesenheit mit 15 min gestempelt → Arbeitszeit 5:55 ≤ 6 h → keine Anhebung."""
    ts = await _punch(
        db,
        tenant,
        employee,
        [
            (EventType.PUNCH_IN, _dt(6, 50)),
            (EventType.BREAK_START, _dt(10, 0)),
            (EventType.BREAK_END, _dt(10, 15)),
            (EventType.PUNCH_OUT, _dt(13, 0)),
        ],
    )
    assert ts.break_minutes == 15
    assert ts.net_work_minutes == 355  # 5:55
    assert ts.status == TimesheetStatus.COMPLETE, ts.flagged_reason


async def test_six_hours_or_less_needs_no_break(db, tenant, employee):
    """Bis 6 h Arbeitszeit: keine Pflichtpause, nichts wird abgezogen."""
    ts = await _punch(
        db,
        tenant,
        employee,
        [(EventType.PUNCH_IN, _dt(8, 0)), (EventType.PUNCH_OUT, _dt(14, 0))],
    )
    assert ts.break_minutes == 0
    assert ts.net_work_minutes == 360


async def test_split_shift_gap_is_not_presence(db, tenant, employee):
    """Zwei Schichten am Kalendertag (Nachtschicht-Split): die Lücke dazwischen
    ist weder Arbeit noch Pause und darf die Pflichtpause nicht hochtreiben.

    Sessions 00:00–04:45 und 19:30–23:59 (15 min Pause abends):
    Anwesenheit = Arbeit + Pausen = 9:09 ≤ 9:30 → 30 min genügen — NICHT 45,
    obwohl die Tagesspanne fast 24 h beträgt."""
    ts = await _punch(
        db,
        tenant,
        employee,
        [
            (EventType.PUNCH_IN, _dt(0, 0)),
            (EventType.PUNCH_OUT, _dt(4, 45)),
            (EventType.PUNCH_IN, _dt(19, 30)),
            (EventType.BREAK_START, _dt(23, 0)),
            (EventType.BREAK_END, _dt(23, 15)),
            (EventType.PUNCH_OUT, _dt(23, 59)),
        ],
    )
    # Arbeit: 285 + (269 − 15) = 539; Anhebung nur 15 → 30 (Arbeitszeit 8:54 ≤ 9 h)
    assert ts.break_minutes == 30
    assert ts.net_work_minutes == 524
    # Kein §4-Flag: die Pause genügt für die echte Anwesenheit.
    assert "Pause" not in (ts.flagged_reason or "")


# ── Betriebliche Pausenregel (ab Stichtag 2026-09-01): > 4 h Arbeit → 45 min ──


def _dt_neu(h, m=0):
    """Wie _dt, aber auf einem Tag NACH dem Stichtag der Betriebsregel."""
    return datetime(2026, 9, 15, h, m, tzinfo=_BERLIN).astimezone(UTC)


async def _punch_neu(db, tenant, employee, seq):
    for et, t in seq:
        await record_punch(
            db=db,
            tenant_id=tenant.id,
            employee_id=employee.id,
            event_type=et,
            source=EventSource.NFC_TERMINAL,
            event_time=t,
        )
    return await recompute_daily_timesheet(db, tenant.id, employee.id, date(2026, 9, 15))


def test_betrieb_rule_pure():
    from zeiterfassung.policy.statutory import effective_break_minutes_betrieb as beb

    stichtag = date(2026, 8, 28)
    davor = date(2026, 8, 27)
    # Vor dem Stichtag: unverändert die gesetzliche Staffel.
    assert beb(480, 0, work_date=davor) == 30
    assert beb(300, 0, work_date=davor) == 0
    # Ab dem Stichtag: > 4 h Arbeit → 45 min, gestempelte Pausen zählen voll.
    assert beb(480, 0, work_date=stichtag) == 45
    assert beb(480, 20, work_date=stichtag) == 45  # 20 gestempelt → 45 angesetzt
    assert beb(480, 50, work_date=stichtag) == 50  # ausreichend → bleibt exakt
    # Exakt 4 h ist bewusst frei (strikt "mehr als"): 4-h-Halbtage ohne Abzug.
    assert beb(240, 0, work_date=stichtag) == 0
    assert beb(241, 0, work_date=stichtag) == 45
    # Wer so viel Pause stempelt, dass die Arbeit unter 4 h fällt, behält sie.
    assert beb(250, 15, work_date=stichtag) == 15


async def test_betrieb_20min_gestempelt_wird_45_und_arbzg_warnung(db, tenant, employee):
    """9:05 Anwesenheit, 20 min gestempelt → 45 min angesetzt (+25), §4-Warnung
    (real nur 20 min statt gesetzlich 30 genommen)."""
    ts = await _punch_neu(
        db,
        tenant,
        employee,
        [
            (EventType.PUNCH_IN, _dt_neu(8, 0)),
            (EventType.BREAK_START, _dt_neu(12, 0)),
            (EventType.BREAK_END, _dt_neu(12, 20)),
            (EventType.PUNCH_OUT, _dt_neu(17, 5)),
        ],
    )
    assert ts.break_minutes == 45
    assert ts.net_work_minutes == 545 - 45  # keine Negativwerte, exakt Anwesenheit − 45
    assert ts.status == TimesheetStatus.FLAGGED
    assert "ArbZG §4" in (ts.flagged_reason or "")
    assert "20 min" in (ts.flagged_reason or "")


async def test_betrieb_35min_gestempelt_wird_45_ohne_warnung(db, tenant, employee):
    """35 min gestempelt: gesetzlich genug (≥ 30) → keine Warnung, aber
    betrieblich auf 45 angehoben."""
    ts = await _punch_neu(
        db,
        tenant,
        employee,
        [
            (EventType.PUNCH_IN, _dt_neu(8, 0)),
            (EventType.BREAK_START, _dt_neu(12, 0)),
            (EventType.BREAK_END, _dt_neu(12, 35)),
            (EventType.PUNCH_OUT, _dt_neu(16, 30)),
        ],
    )
    assert ts.break_minutes == 45
    assert ts.net_work_minutes == 510 - 45
    assert ts.status == TimesheetStatus.COMPLETE, ts.flagged_reason


async def test_betrieb_exakt_4h_bleibt_ohne_abzug(db, tenant, employee):
    """Exakt 4:00 gestempelt (Halbtag) → kein Abzug, kein Flag."""
    ts = await _punch_neu(
        db,
        tenant,
        employee,
        [(EventType.PUNCH_IN, _dt_neu(8, 0)), (EventType.PUNCH_OUT, _dt_neu(12, 0))],
    )
    assert ts.break_minutes == 0
    assert ts.net_work_minutes == 240
    assert ts.status == TimesheetStatus.COMPLETE, ts.flagged_reason


async def test_betrieb_4h30_ohne_pause_45_ohne_arbzg_warnung(db, tenant, employee):
    """4:30 ohne Pause → 45 min angesetzt (betrieblich), aber KEINE §4-Warnung:
    gesetzlich wäre bei ≤ 6 h Arbeit gar keine Pause nötig."""
    ts = await _punch_neu(
        db,
        tenant,
        employee,
        [(EventType.PUNCH_IN, _dt_neu(8, 0)), (EventType.PUNCH_OUT, _dt_neu(12, 30))],
    )
    assert ts.break_minutes == 45
    assert ts.net_work_minutes == 270 - 45
    assert ts.status == TimesheetStatus.COMPLETE, ts.flagged_reason


async def test_betrieb_gilt_nicht_vor_stichtag(db, tenant, employee):
    """Tag vor dem Stichtag (27.08.): alte Regel (30 min), keine §4-Warnung —
    Korrekturen an Alt-Tagen rechnen und flaggen wie zum damaligen Stand."""

    def _dt_alt(h, m=0):
        return datetime(2026, 8, 27, h, m, tzinfo=_BERLIN).astimezone(UTC)

    for et, t in [
        (EventType.PUNCH_IN, _dt_alt(8, 0)),
        (EventType.BREAK_START, _dt_alt(12, 0)),
        (EventType.BREAK_END, _dt_alt(12, 20)),
        (EventType.PUNCH_OUT, _dt_alt(17, 5)),
    ]:
        await record_punch(
            db=db,
            tenant_id=tenant.id,
            employee_id=employee.id,
            event_type=et,
            source=EventSource.NFC_TERMINAL,
            event_time=t,
        )
    ts = await recompute_daily_timesheet(db, tenant.id, employee.id, date(2026, 8, 27))
    assert ts.break_minutes == 30
    assert ts.net_work_minutes == 545 - 30
    assert ts.status == TimesheetStatus.COMPLETE, ts.flagged_reason


async def test_anhebung_erst_am_tagesende(db, tenant, employee):
    """Offener Tag (kein Gehen): nur die gestempelten Pausen — die Anhebung
    wird erst beim Tagesabschluss dazugebucht."""
    for et, t in [
        (EventType.PUNCH_IN, _dt_neu(8, 0)),
        (EventType.BREAK_START, _dt_neu(12, 0)),
        (EventType.BREAK_END, _dt_neu(12, 20)),
    ]:
        await record_punch(
            db=db,
            tenant_id=tenant.id,
            employee_id=employee.id,
            event_type=et,
            source=EventSource.NFC_TERMINAL,
            event_time=t,
        )
    ts = await recompute_daily_timesheet(db, tenant.id, employee.id, date(2026, 9, 15))
    assert ts.break_minutes == 20  # NICHT auf 45 angehoben — Tag läuft noch
    # Kein §4-Flag mitten am Tag: die Pause kann noch kommen.
    assert "ArbZG §4: Pause zu kurz gestempelt" not in (ts.flagged_reason or "")


async def test_pausen_aufschlag_override(db, tenant, employee):
    """HR-Korrektur: der Aufschlag ist pro Tag nach oben/unten setzbar und
    überlebt den Recompute; die ArbZG-Warnung (gestempelte Basis) bleibt."""
    ts = await _punch_neu(
        db,
        tenant,
        employee,
        [
            (EventType.PUNCH_IN, _dt_neu(8, 0)),
            (EventType.BREAK_START, _dt_neu(12, 0)),
            (EventType.BREAK_END, _dt_neu(12, 20)),
            (EventType.PUNCH_OUT, _dt_neu(17, 5)),
        ],
    )
    assert ts.break_minutes == 45  # Automatik: 20 gestempelt + 25 Aufschlag

    # Runter korrigieren: Aufschlag 5 → 25 min Pause gesamt.
    ts.break_auto_override_minutes = 5
    await db.flush()
    ts = await recompute_daily_timesheet(db, tenant.id, employee.id, date(2026, 9, 15))
    assert ts.break_minutes == 25
    assert ts.net_work_minutes == 545 - 25
    assert "ArbZG §4" in (ts.flagged_reason or "")  # real nur 20 min genommen

    # Hoch korrigieren: Aufschlag 60 → 80 min gesamt.
    ts.break_auto_override_minutes = 60
    await db.flush()
    ts = await recompute_daily_timesheet(db, tenant.id, employee.id, date(2026, 9, 15))
    assert ts.break_minutes == 80
    assert ts.net_work_minutes == 545 - 80


async def test_pausen_aufschlag_override_auch_vor_stichtag(db, tenant, employee):
    """Auch Alt-Tage (gesetzliche Anhebung): der Aufschlag ist sichtbar
    editierbar — Override wirkt datumsunabhängig, nur die AUTOMATIK-Höhe
    hängt am Stichtag."""

    def _dt_alt(h, m=0):
        return datetime(2026, 8, 26, h, m, tzinfo=_BERLIN).astimezone(UTC)

    for et, t in [
        (EventType.PUNCH_IN, _dt_alt(8, 0)),
        (EventType.BREAK_START, _dt_alt(12, 0)),
        (EventType.BREAK_END, _dt_alt(12, 19)),
        (EventType.PUNCH_OUT, _dt_alt(17, 5)),
    ]:
        await record_punch(
            db=db,
            tenant_id=tenant.id,
            employee_id=employee.id,
            event_type=et,
            source=EventSource.NFC_TERMINAL,
            event_time=t,
        )
    ts = await recompute_daily_timesheet(db, tenant.id, employee.id, date(2026, 8, 26))
    assert ts.break_minutes == 30  # Automatik: gesetzliche Staffel (19 → 30)

    ts.break_auto_override_minutes = 26  # HR: Aufschlag hoch auf 26 → 45 gesamt
    await db.flush()
    ts = await recompute_daily_timesheet(db, tenant.id, employee.id, date(2026, 8, 26))
    assert ts.break_minutes == 19 + 26
    assert ts.net_work_minutes == 545 - 45

    ts.break_auto_override_minutes = 0  # HR: kein Aufschlag → nur gestempelt
    await db.flush()
    ts = await recompute_daily_timesheet(db, tenant.id, employee.id, date(2026, 8, 26))
    assert ts.break_minutes == 19
    # Alt-Tag: keine neue §4-Warnung (Warnungen starten erst mit dem Stichtag).
    assert "ArbZG §4: Pause zu kurz gestempelt" not in (ts.flagged_reason or "")
