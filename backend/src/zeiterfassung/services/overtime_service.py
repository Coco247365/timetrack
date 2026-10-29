"""Überstunden-Konto: Netto-Ansammlung + Auszahlung, als datiertes Log.

Kernregeln:
- Ansammlung pro Monat = NETTO (Ist − Soll über die erfassten Tage). +2/−2 = 0;
  weniger als Soll ergibt Minusstunden. Soll/Tag = Wochenstunden ÷ 5; an Feiertagen/
  Wochenenden ist das Soll 0 (Feiertag deckt die Stunden ab). Der Saldo DARF negativ sein.
- Auszahlung je nach Modus des Mitarbeiters (none | all | above_cap |
  forfeit_above_cap = Ueberlauf verfaellt) beim Monatsabschluss;
  ausgezahlt wird nur positiver Überhang, Minus bleibt als Schuld auf dem Konto.
- Alles ist ein datierter, unveränderlicher Log-Eintrag; der Saldo = Replay des Logs.
  Dadurch bleiben vergangene Monate stabil und Exporte rückrechenbar.
"""

from __future__ import annotations

import logging
import uuid
from calendar import monthrange
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from zeiterfassung.config import get_settings
from zeiterfassung.core.holidays import get_holidays
from zeiterfassung.db.scoping import get_scoped, tenant_scoped
from zeiterfassung.models.clock import DailyTimesheet
from zeiterfassung.models.overtime import OvertimeEntry
from zeiterfassung.models.payroll_booking import PayrollBooking
from zeiterfassung.models.user import User
from zeiterfassung.models.work_time_profile import WorkTimeProfile
from zeiterfassung.policy import overtime as policy_overtime
from zeiterfassung.policy import tagesregeln as policy_tagesregeln
from zeiterfassung.services import audit_service
from zeiterfassung.services.tenant_service import tenant_local_date

# 4 Nachkommastellen: eine Minute ist 1/60 h = 0,01667 h — auf Hundertstel
# gerundet (0,6-min-Raster) kippte die h:min-Anzeige um ±1 Minute.
PRAEZISION = Decimal("0.0001")
_TYPE_ORDER = {"adjustment": 0, "accrual": 1, "payout": 2, "forfeit": 3}


async def _daily_rows_for_month(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    user: User,
    year: int,
    month: int,
    *,
    today: date | None = None,
    bundesland: str = "HH",
    stats_out: dict | None = None,
) -> list[dict]:
    """Ist−Soll je Tag des Monats als Zeilen — beschafft nur die Daten.

    Die *fachliche* Tagesregel (Deckung, unentschuldigtes Fehlen, Halbtag, Stichtag)
    liegt in :func:`zeiterfassung.policy.overtime.classify_day`. Diese Funktion lädt
    Tagesbögen, Abwesenheiten und Feiertage, baut daraus je Tag ein ``DayFacts`` und
    lässt die Policy entscheiden.

    Rückgabe je Zeile: ``work_date``, ``net_minutes`` (Ist), ``target_minutes`` (effektives
    Soll nach Abwesenheit), ``delta_minutes`` (Ist − Soll), ``weekend``, ``holiday``,
    ``absence_fraction``, ``absent`` (True = unentschuldigtes Fehlen).
    """
    if today is None:
        today = datetime.now(tz=UTC).date()
    ym = date(year, month, 1)
    _, last = monthrange(year, month)
    month_end = date(year, month, last)
    # Auto-Erfassung ist immer gefüllt → nie unentschuldigt. Bei NFC/web zählt ein
    # vergangener Werktag ohne erfasste Zeit als unentschuldigtes Fehlen.
    is_not_auto = (user.time_capture or "").lower() != "auto"
    # Stichtag = Erfassungsbeginn (ab wann das Programm zaehlt), sonst Firmeneintritt.
    start_cutoff = user.counting_start_date or user.hire_date
    # Austritt: ab dem Austrittsdatum zaehlt kein Tag mehr (keine Fehlzeit-Minus).
    end_cutoff = user.termination_date
    holidays = get_holidays(year, bundesland)
    # Profil-Regel "wochenende_ins_konto": zaehlt WE-/Feiertagsarbeit dieses
    # Mitarbeiters normal ins Konto (statt wie im Standard-Fall #21 ueber die
    # eigene Zuschlags-Lohnart)? Profile einmal pro Monatsberechnung laden.
    wage_profiles = await _tenant_wage_profiles(db, tenant_id)
    weekend_counts = _resolve_weekend_counts(wage_profiles, user.wage_profile_id)
    # Arbeitszeit-Profil (kind 'auto', Spec 2026-08-14): ein aktives Auto-
    # Profil ersetzt das Tages-Soll komplett (Fenster − Pause je Wochentag,
    # freier Profil-Tag = Soll 0 → KEIN Minus). Einmal je Monat laden.
    wt_profile = None
    if user.work_time_profile_id is not None:
        wt_profile = await get_scoped(db, WorkTimeProfile, tenant_id, user.work_time_profile_id)
    regeln = policy_tagesregeln.Tagesregeln.fuer(user, wt_profile)

    ts_rows = (
        await db.execute(
            tenant_scoped(
                select(
                    DailyTimesheet.work_date,
                    DailyTimesheet.net_work_minutes,
                    DailyTimesheet.is_holiday_work,
                    DailyTimesheet.start_time,
                    DailyTimesheet.weekend_booking,
                ),
                DailyTimesheet,
                tenant_id,
            ).where(
                DailyTimesheet.employee_id == user.id,
                DailyTimesheet.work_date >= ym,
                DailyTimesheet.work_date <= month_end,
            )
        )
    ).all()
    ts_by_date = {r.work_date: r for r in ts_rows}

    # Genehmigte Abwesenheiten des Monats → je Tag der abgedeckte Soll-Anteil (0..1).
    from zeiterfassung.models.absence import (
        ApprovalRequest,
        RequestStatus,
        RequestType,
    )

    absence_rows = (
        (
            await db.execute(
                tenant_scoped(select(ApprovalRequest), ApprovalRequest, tenant_id).where(
                    ApprovalRequest.employee_id == user.id,
                    ApprovalRequest.status == RequestStatus.APPROVED,
                    ApprovalRequest.request_type.in_(
                        [
                            RequestType.VACATION,
                            RequestType.SICK_LEAVE,
                            RequestType.SPECIAL_LEAVE,
                            RequestType.UNPAID_LEAVE,
                            RequestType.OVERTIME_REDUCTION,
                        ]
                    ),
                    ApprovalRequest.date_from <= month_end,
                    ApprovalRequest.date_to >= ym,
                )
            )
        )
        .scalars()
        .all()
    )
    covered: dict[date, float] = {}
    # Urlaubs-Anteil separat: wird am Tag gearbeitet, deckt Urlaub das Soll nicht
    # (Tag zählt normal, Urlaub wird zurückgegeben — siehe policy.classify_day, #22).
    covered_vacation: dict[date, float] = {}
    # Überstundenabbau ist KEINE Deckung (Soll bleibt offen → Minus), steuert
    # aber die Einstufung: nicht "unentschuldigt" + zählt ohne Tagesbogen.
    covered_reduction: dict[date, float] = {}
    for a in absence_rows:
        d = a.date_from
        # Deckungs-Anteil: EINE Stelle fuer alle Konsumenten (policy.tagesregeln).
        frac = policy_tagesregeln.Tagesregeln.abwesenheits_deckung(a)
        is_vacation = a.request_type == RequestType.VACATION
        is_reduction = a.request_type == RequestType.OVERTIME_REDUCTION
        while d <= a.date_to:
            if ym <= d <= month_end:
                if is_reduction:
                    covered_reduction[d] = min(1.0, covered_reduction.get(d, 0.0) + frac)
                else:
                    covered[d] = min(1.0, covered.get(d, 0.0) + frac)
                    if is_vacation:
                        covered_vacation[d] = min(1.0, covered_vacation.get(d, 0.0) + frac)
            d += timedelta(days=1)

    # Reine Tagesregel liegt in policy/overtime.classify_day — hier nur Fakten sammeln.
    out: list[dict] = []
    day = ym
    while day <= month_end:
        weekend = day.weekday() >= 5  # Sa/So
        ts = ts_by_date.get(day)
        net = int(ts.net_work_minutes or 0) if ts is not None else 0
        holiday_eff = (day in holidays) or (ts is not None and bool(ts.is_holiday_work))
        facts = policy_overtime.DayFacts(
            day=day,
            weekend=weekend,
            holiday=holiday_eff,
            coverage=covered.get(day, 0.0),
            net_minutes=net,
            has_timesheet=ts is not None,
            worked=ts is not None and not (net == 0 and ts.start_time is None),
            is_auto=not is_not_auto,
            is_past=day < today,
            is_today_or_past=day <= today,
            within_cutoff=(start_cutoff is None or day >= start_cutoff)
            and (end_cutoff is None or day <= end_cutoff),
            vacation_coverage=covered_vacation.get(day, 0.0),
            reduction=covered_reduction.get(day, 0.0),
            weekend_booking=(ts.weekend_booking if ts is not None else None),
        )
        # Tages-Soll: EINE Stelle fuer alle Konsumenten (policy.tagesregeln) —
        # Profil-Vorrang, Wochentagsplan, Wochenstunden-Fallback stecken dort.
        soll = regeln.tagessoll(day)
        res = policy_overtime.classify_day(
            facts,
            soll,
            weekend_counts=weekend_counts,
            hat_soll=policy_overtime.hat_tagessoll(user.employment_type),
        )
        # Bruttosollzeit (Alt-System-Vokabular, gleiche Definition wie im
        # Stundenzettel/Monatsabgleich #279): Tagessoll aller Mo–Fr-Tage VOR
        # dem Fehlzeiten-Abzug — zählt auch voll gedeckte Tage und WERKTAGS-
        # FEIERTAGE mit (Altsystem-Kette: Brutto − Feiertag − Urlaub − Krank − …
        # = effektives Soll). Wochenende = 0, vor Erfassungsbeginn/nach
        # Austritt nichts. Es zählt jeder Tag, der auch im effektiven Soll
        # steckt (res — inklusive Sonderfällen wie gestempelten Zukunfts-
        # tagen; sonst könnte Brutto < effektiv werden), plus die VERGANGENEN
        # Tage ohne Konto-Zeile (voll gedeckt bzw. Feiertag). Der heutige
        # (noch laufende) Arbeitstag zählt wie im Konto erst morgen.
        if (
            stats_out is not None
            and facts.within_cutoff
            and not weekend
            and (res is not None or day < today)
        ):
            stats_out["gross_soll_minutes"] = stats_out.get("gross_soll_minutes", 0) + soll
        if res is not None:
            out.append(
                {
                    "work_date": day,
                    "net_minutes": res.net_minutes,
                    "target_minutes": res.target_minutes,
                    "delta_minutes": res.delta_minutes,
                    "weekend": weekend,
                    "holiday": holiday_eff,
                    "absence_fraction": res.coverage,
                    "absent": res.absent,
                    # Geplanter Überstundenabbau — der Verlauf soll den Tag so
                    # nennen, nicht als "Arbeit" mit raetselhaftem Minus.
                    "reduction": facts.reduction,
                    "weekend_booking": ts.weekend_booking if ts is not None else None,
                }
            )
        day += timedelta(days=1)
    return out


async def _net_overtime_minutes_for_month(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    user: User,
    year: int,
    month: int,
    *,
    today: date | None = None,
    bundesland: str = "HH",
) -> int:
    """Netto Ist−Soll (min) über die Tage des Monats (Summe der Tageszeilen)."""
    rows = await _daily_rows_for_month(
        db, tenant_id, user, year, month, today=today, bundesland=bundesland
    )
    return sum(r["delta_minutes"] for r in rows)


async def month_summary(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    user: User,
    year: int,
    month: int,
    *,
    today: date | None = None,
) -> dict:
    """Monats-Summen des Arbeitszeitkontos: Ist, effektives Soll, Saldo (Ist − Soll).

    Für die Kalenderköpfe. Summiert die Tageszeilen der Konto-Policy
    (:func:`_daily_rows_for_month`): Feiertage/Abwesenheiten reduzieren das
    Soll, unentschuldigte Fehltage zählen als Minus, Zukunftstage zählen nicht
    — im laufenden Monat also „Soll bis heute". Monat vor dem Stichtag → 0.
    """
    bundesland = await _tenant_bundesland(db, tenant_id)
    stats: dict = {}
    rows = await _daily_rows_for_month(
        db, tenant_id, user, year, month, today=today, bundesland=bundesland, stats_out=stats
    )
    ist = sum(r["net_minutes"] for r in rows)
    soll = sum(r["target_minutes"] for r in rows)
    return {
        "year": year,
        "month": month,
        "ist_minutes": ist,
        "soll_minutes": soll,
        # Soll vor Fehlzeiten-Abzug (Arbeitstage × Tagessoll bis heute) — der
        # Abgleichswert „Bruttosollzeit" aus den Alt-System-PDFs.
        "brutto_soll_minutes": int(stats.get("gross_soll_minutes", 0)),
        "saldo_minutes": sum(r["delta_minutes"] for r in rows),
        # ist − soll kann vom Saldo abweichen, falls die Policy einzelne Tage
        # anders bewertet — der Saldo ist die konto-konsistente Größe.
    }


async def _account_start(db: AsyncSession, tenant_id: uuid.UUID, user: User) -> date | None:
    """Kontobeginn = Stichtag (hire_date) bzw. – falls früher erfasst – erster Tagesbogen."""
    # Erfassungsbeginn (sonst Firmeneintritt) = strikter Stichtag; alles davor wird
    # ignoriert. Ist keiner gesetzt, zählt das Konto ab dem ersten erfassten Tagesbogen.
    cutoff = user.counting_start_date or user.hire_date
    if cutoff is not None:
        return cutoff
    return (
        await db.execute(
            tenant_scoped(
                select(func.min(DailyTimesheet.work_date)), DailyTimesheet, tenant_id
            ).where(
                DailyTimesheet.employee_id == user.id,
            )
        )
    ).scalar()


async def _tenant_bundesland(db: AsyncSession, tenant_id: uuid.UUID) -> str:
    """Bundesland des Mandanten für den Feiertagskalender (Default HH)."""
    from zeiterfassung.models.tenant import Tenant

    bl = (await db.execute(select(Tenant.bundesland).where(Tenant.id == tenant_id))).scalar()
    return bl or "HH"


async def _tenant_today(db: AsyncSession, tenant_id: uuid.UUID) -> date:
    """Tenant-lokales „heute" (Europe/Berlin o. Ä.) — siehe ``tenant_local_date``.

    Selbe Ableitung wie in ``clock_service`` (#3/#140): UTC-Kalendertag
    ist bei Punches nahe Mitternacht falsch, hier für den offenen Rand von
    ``live_balance`` genauso.
    """
    from zeiterfassung.models.tenant import Tenant

    tenant = (await db.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one_or_none()
    return tenant_local_date(datetime.now(tz=UTC), tenant)


async def _tenant_wage_profiles(db: AsyncSession, tenant_id: uuid.UUID) -> dict[str, dict]:
    """Abrechnungsprofile des Mandanten als dict (Profil-ID -> Profil)."""
    from zeiterfassung.models.tenant import Tenant

    settings = (await db.execute(select(Tenant.settings).where(Tenant.id == tenant_id))).scalar()
    return {str(p.get("id")): p for p in ((settings or {}).get("wage_profiles") or [])}


def _resolve_weekend_counts(profiles: dict[str, dict], wage_profile_id: str | None) -> bool:
    """Profil-Regel ``wochenende_ins_konto``: zaehlt Wochenend-/Feiertagsarbeit
    dieses Mitarbeiters normal ins Ueberstundenkonto (statt wie im Standard-Fall
    #21 ueber die eigene Zuschlags-Lohnart)? Default (kein Profil/keine Regel) = False.
    """
    if not wage_profile_id:
        return False
    profile = profiles.get(wage_profile_id)
    if not profile:
        return False
    return bool(profile.get("wochenende_ins_konto", False))


async def unexcused_days(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    users: list[User],
    date_from: date,
    date_to: date,
    *,
    today: date | None = None,
) -> list[tuple[uuid.UUID, date]]:
    """Unentschuldigte Fehltage (Mitarbeiter, Datum) im Zeitraum.

    Nutzt dieselbe Tagesregel wie der Mitarbeiter-Verlauf
    (``policy.overtime.classify_day`` ueber ``_daily_rows_for_month``) — die
    Fachlogik bleibt an einer Stelle. Fuer den Teamkalender (rotes „U").
    Kosten: eine Monatsberechnung je Mitarbeiter und beruehrtem Monat.
    """
    if today is None:
        today = datetime.now(tz=UTC).date()
    bundesland = await _tenant_bundesland(db, tenant_id)
    months: list[tuple[int, int]] = []
    y, m = date_from.year, date_from.month
    while (y, m) <= (date_to.year, date_to.month) and len(months) < 24:
        months.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)

    out: list[tuple[uuid.UUID, date]] = []
    for user in users:
        for yy, mm in months:
            for r in await _daily_rows_for_month(
                db, tenant_id, user, yy, mm, today=today, bundesland=bundesland
            ):
                if r["absent"] and date_from <= r["work_date"] <= date_to:
                    out.append((user.id, r["work_date"]))
    return out


async def daily_breakdown(
    db: AsyncSession, tenant_id: uuid.UUID, user: User, *, today: date | None = None
) -> list[dict]:
    """Tages-Verlauf des Arbeitszeitkontos ab Stichtag bis heute, mit laufendem Saldo.

    Ein chronologischer Ledger: je gearbeitetem Tag eine Zeile (Ist, Soll, Δ), dazu die
    manuellen Korrekturen an ihrem Datum. Der laufende Saldo (``balance_hours``) baut sich
    Zeile für Zeile auf und endet exakt auf dem Karten-Saldo (``live_balance``).

    Zeilenformat:
      ``date``, ``type`` ("day" | "adjustment" | "payout"), ``worked_hours``,
      ``target_hours``, ``delta_hours``, ``balance_hours`` sowie bei Tagen
      ``weekend``/``holiday``/``absence_fraction`` und bei Ledger-Zeilen
      ``reason`` (Korrekturen zusätzlich ``id``).

    Der Verlauf ist IMMER tagesgenau — auch für abgeschlossene Monate (inkl.
    unentschuldigter Fehltage). Auszahlungen erscheinen als eigene Zeile,
    Monatsansammlungen NICHT (ihre Tage stehen ja einzeln da). Damit endet der
    Verlauf auf dem Karten-Saldo (``live_balance``), solange abgeschlossene
    Monate nicht rückwirkend verändert wurden; danach zeigt der Verlauf die
    heutige Tagesrechnung, die Karte die eingefrorene Buchung.
    """
    start = await _account_start(db, tenant_id, user)
    bundesland = await _tenant_bundesland(db, tenant_id)
    entries = await _all_entries(db, tenant_id, user.id)

    if today is None:
        today = datetime.now(tz=UTC).date()
    events: list[dict] = []
    if start is not None:
        y, m = start.year, start.month
        guard = 0
        while (y, m) <= (today.year, today.month) and guard < 600:
            for r in await _daily_rows_for_month(
                db, tenant_id, user, y, m, today=today, bundesland=bundesland
            ):
                events.append(
                    {
                        "date": r["work_date"],
                        "type": "day",
                        # Minuten sind die Rechengröße; Stunden nur fürs JSON. 4 Dezimalen
                        # statt 2, sonst wird z. B. −5 min zu −0,08 h (= −4,8 min).
                        "_delta_min": float(r["delta_minutes"]),
                        "worked_hours": round(r["net_minutes"] / 60, 4),
                        "target_hours": round(r["target_minutes"] / 60, 4),
                        "delta_hours": round(r["delta_minutes"] / 60, 4),
                        "weekend": r["weekend"],
                        "holiday": r["holiday"],
                        "absence_fraction": r["absence_fraction"],
                        "absent": r["absent"],
                        "reduction": r.get("reduction", 0.0),
                        "weekend_booking": r.get("weekend_booking"),
                    }
                )
            m += 1
            if m > 12:
                m, y = 1, y + 1
            guard += 1

    # Ledger-Zeilen an ihrem Buchungsdatum einreihen: Korrekturen und
    # Auszahlungen. Ansammlungen NICHT — deren Tage stehen einzeln im Verlauf.
    for e in entries:
        if e.entry_type == "adjustment":
            events.append(
                {
                    "date": e.entry_date,
                    "type": "adjustment",
                    "_delta_min": float(e.hours) * 60,
                    "delta_hours": float(e.hours),
                    "reason": e.reason,
                    "id": str(e.id),
                }
            )
        elif e.entry_type in ("payout", "forfeit"):
            events.append(
                {
                    "date": e.entry_date,
                    "type": e.entry_type,
                    "_delta_min": float(e.hours) * 60,
                    "delta_hours": float(e.hours),
                    "reason": e.reason,
                }
            )

    # Chronologisch; gleicher Tag: erst Tageswert, dann Ledger-Zeilen in Buchungsreihenfolge.
    events.sort(
        key=lambda x: (
            x["date"],
            0 if x["type"] == "day" else 1,
            _TYPE_ORDER.get(x["type"], 9),
        )
    )
    # Laufender Saldo in MINUTEN — Stunden-Rundung je Zeile würde sich sonst
    # aufsummieren (25+25−5 min ≠ 0,42+0,42−0,08 h → „46 min statt 45").
    running_min = 0.0
    out: list[dict] = []
    for ev in events:
        running_min += ev.pop("_delta_min")
        out.append(
            {**ev, "date": ev["date"].isoformat(), "balance_hours": round(running_min / 60, 4)}
        )
    return out


def _replay(entries: list[OvertimeEntry]) -> Decimal:
    """Saldo (h) = Summe aller Log-Einträge in Datumsreihenfolge.

    Minusstunden werden geführt: der Saldo darf negativ sein.
    """
    bal = Decimal("0")
    for e in sorted(
        entries,
        key=lambda x: (x.year_month, x.entry_date, _TYPE_ORDER.get(x.entry_type, 9), x.created_at),
    ):
        bal += Decimal(str(e.hours))
    return bal.quantize(PRAEZISION)


async def _all_entries(
    db: AsyncSession, tenant_id: uuid.UUID, employee_id: uuid.UUID
) -> list[OvertimeEntry]:
    return list(
        (
            await db.execute(
                tenant_scoped(select(OvertimeEntry), OvertimeEntry, tenant_id).where(
                    OvertimeEntry.employee_id == employee_id,
                )
            )
        )
        .scalars()
        .all()
    )


async def current_balance(
    db: AsyncSession, tenant_id: uuid.UUID, employee_id: uuid.UUID
) -> Decimal:
    """Aktueller Überstunden-Saldo (h) aus dem gesamten Log."""
    return _replay(await _all_entries(db, tenant_id, employee_id))


def _settled_months(entries: list[OvertimeEntry]) -> set[date]:
    """Monate mit accrual-Zeile = abgeschlossen; ihr Netto steckt bereits im Ledger."""
    return {e.year_month for e in entries if e.entry_type == "accrual"}


async def live_balance(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    user: User,
    *,
    today: date | None = None,
    entries: list[OvertimeEntry] | None = None,
    bundesland: str | None = None,
) -> Decimal:
    """Laufender Arbeitszeitsaldo seit Eintritt — Ledger + offene Monate live.

    = Replay des Ledgers (Ansammlungen abgeschlossener Monate, Auszahlungen,
      manuelle Korrekturen) + Ist−Soll je Tag über die noch NICHT abgeschlossenen
      Monate. Eine Wahrheit: ein per ``settle_month`` eingefrorener Monat wird
      nie neu gerechnet (Lock, BetrVG-Nachvollziehbarkeit), nur der offene Rand
      kommt minutengenau aus den Tagesbögen. Solange kein Monat abgeschlossen
      wurde, ist das Ergebnis identisch zur reinen Tagesrechnung (der Ledger
      enthält dann nur Korrekturen).

    ``today``/``entries``/``bundesland`` sind injizierbar (Tests bzw.
    ``live_balances_for_tenant``, das den Ledger für alle Mitarbeiter in einer
    Query lädt).
    """
    if entries is None:
        entries = await _all_entries(db, tenant_id, user.id)
    settled = _settled_months(entries)
    ledger = _replay(entries)

    start = await _account_start(db, tenant_id, user)
    if bundesland is None:
        bundesland = await _tenant_bundesland(db, tenant_id)
    total_min = 0
    if start is not None:
        if today is None:
            today = await _tenant_today(db, tenant_id)
        y, m = start.year, start.month
        guard = 0
        while (y, m) <= (today.year, today.month) and guard < 600:
            if date(y, m, 1) not in settled:
                total_min += await _net_overtime_minutes_for_month(
                    db, tenant_id, user, y, m, today=today, bundesland=bundesland
                )
            m += 1
            if m > 12:
                m = 1
                y += 1
            guard += 1
    return (ledger + Decimal(total_min) / Decimal(60)).quantize(PRAEZISION)


async def live_balances_for_tenant(
    db: AsyncSession, tenant_id: uuid.UUID
) -> dict[uuid.UUID, Decimal]:
    """Laufender Ist-Saldo (h) je Mitarbeiter für den ganzen Mandanten.

    Der Ledger wird EINMAL für den Mandanten geladen (statt je Mitarbeiter);
    live gerechnet werden pro Mitarbeiter nur die nicht abgeschlossenen Monate.
    """
    from collections import defaultdict

    users = (await db.execute(tenant_scoped(select(User), User, tenant_id))).scalars().all()
    rows = (
        (await db.execute(tenant_scoped(select(OvertimeEntry), OvertimeEntry, tenant_id)))
        .scalars()
        .all()
    )
    by_emp: dict[uuid.UUID, list[OvertimeEntry]] = defaultdict(list)
    for r in rows:
        by_emp[r.employee_id].append(r)
    bundesland = await _tenant_bundesland(db, tenant_id)
    today = await _tenant_today(db, tenant_id)
    return {
        u.id: await live_balance(
            db,
            tenant_id,
            u,
            today=today,
            entries=by_emp.get(u.id, []),
            bundesland=bundesland,
        )
        for u in users
    }


async def _is_month_settled(
    db: AsyncSession, tenant_id: uuid.UUID, employee_id: uuid.UUID, ym: date
) -> bool:
    hit = (
        await db.execute(
            tenant_scoped(select(OvertimeEntry.id), OvertimeEntry, tenant_id).where(
                OvertimeEntry.employee_id == employee_id,
                OvertimeEntry.year_month == ym,
                OvertimeEntry.entry_type == "accrual",
            )
        )
    ).first()
    return hit is not None


async def settle_month(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    user: User,
    year: int,
    month: int,
    actor_id: uuid.UUID | None = None,
) -> None:
    """EINEN Monat abschließen: Netto-Ansammlung + Auszahlung laut Modus. Idempotent.

    Setzt voraus, dass frühere Monate bereits abgeschlossen sind (siehe settle_through).
    """
    ym = date(year, month, 1)
    # Erfassung ohne Soll (Werkstudent/Aushilfe): kein Zeitkonto, keine
    # Ansammlung, keine Auszahlung. Ohne diesen Schutz waere delta = Ist − 0,
    # der Abschluss buchte die komplette Ist-Zeit als Ueberstunden und zahlte
    # sie (Modus above_cap) bis in den DATEV-Export aus.
    if not policy_overtime.hat_tagessoll(user.employment_type):
        return
    if await _is_month_settled(db, tenant_id, user.id, ym):
        return  # bereits eingefroren -> nichts tun (Lock)

    _, last = monthrange(year, month)
    month_end = date(year, month, last)
    now = datetime.now(tz=UTC)

    bundesland = await _tenant_bundesland(db, tenant_id)
    net_min = await _net_overtime_minutes_for_month(
        db, tenant_id, user, year, month, bundesland=bundesland
    )
    net_h = (Decimal(net_min) / Decimal(60)).quantize(PRAEZISION)

    db.add(
        OvertimeEntry(
            tenant_id=tenant_id,
            employee_id=user.id,
            year_month=ym,
            entry_date=month_end,
            entry_type="accrual",
            hours=net_h,
            reason="Monatsansammlung (netto)",
            created_by=actor_id,
            created_at=now,
        )
    )
    await db.flush()

    # Saldo nach dieser Ansammlung — NUR Eintraege bis einschliesslich dieses
    # Monats: beim rueckwirkenden Abschliessen (Backfill) existieren sonst
    # schon Folgemonats-Ansammlungen, und der gesamte Ueberlauf wuerde
    # faelschlich in diesen Monat ausgezahlt.
    bal = _replay([e for e in await _all_entries(db, tenant_id, user.id) if e.year_month <= ym])

    mode = (user.overtime_payout_mode or "above_cap").lower()
    cap = Decimal(str(user.overtime_cap_hours or 0))
    payout = Decimal("0")
    if mode == "all":
        payout = bal
    elif mode in ("above_cap", "forfeit_above_cap"):
        payout = bal - cap
    if payout <= 0:
        return

    payout = payout.quantize(PRAEZISION)
    later = datetime.now(tz=UTC)

    # Verfall statt Auszahlung: der Überlauf verlässt das Konto ebenfalls, wird
    # aber NICHT vergütet — deshalb eigener Eintragstyp und keine Lohnbuchung.
    # Als sichtbare Zeile im Log, nicht als stille Kappung: sonst ist die Frage
    # "wo sind meine drei Stunden hin?" nicht zu beantworten (BetrVG §87).
    if mode == "forfeit_above_cap":
        db.add(
            OvertimeEntry(
                tenant_id=tenant_id,
                employee_id=user.id,
                year_month=ym,
                entry_date=month_end,
                entry_type="forfeit",
                hours=-payout,
                reason=f"Verfallen über {cap} h (nicht ausgezahlt)",
                created_by=actor_id,
                created_at=later,
            )
        )
        await db.flush()
        return

    # Monate vor dem Lohnlauf-Stichtag hat das Altsystem  bezahlt:
    # der Ueberlauf verlaesst das Konto als Auszahlung, aber OHNE Lohnbuchung —
    # sonst liefe die bereits vergütete Zeit ein zweites Mal in den Export.
    cutoff = get_settings().payroll_cutoff_date
    altsystem = cutoff is not None and ym < cutoff
    db.add(
        OvertimeEntry(
            tenant_id=tenant_id,
            employee_id=user.id,
            year_month=ym,
            entry_date=month_end,
            entry_type="payout",
            hours=-payout,
            reason=(
                f"Auszahlung über {cap} h — im Altsystem abgerechnet, keine Lohnbuchung im Tool"
                if altsystem
                else f"Auszahlung (Modus {mode})"
            ),
            created_by=actor_id,
            created_at=later,
        )
    )
    if mode != "all" and not altsystem:
        # Auszahlung erscheint im Lohn-Export als UEBERSTUNDEN-Buchung dieses Monats.
        db.add(
            PayrollBooking(
                tenant_id=tenant_id,
                employee_id=user.id,
                year_month=ym,
                booking_date=month_end,
                wage_type_code="UEBERSTUNDEN_AUSZAHLUNG",
                value=payout,
                note="Überstunden-Auszahlung",
                created_by=actor_id,
                created_at=later,
                updated_at=later,
            )
        )
    # mode=all: keine PayrollBooking hier – die Auszahlung wird im Soll-Export
    # über die Wochen-Staffel gebucht (s. Spec). Der Ledger-payout oben bleibt
    # für ALLE Modi bestehen, damit das Konto ausgeglichen wird.
    await db.flush()


async def settle_through(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    employee_id: uuid.UUID,
    year: int,
    month: int,
    actor_id: uuid.UUID | None = None,
) -> None:
    """Alle offenen Monate ab einem sinnvollen Startpunkt bis (year, month) abschließen.

    Startpunkt = Kontobeginn (Stichtag/Firmeneintritt, sonst frühester Tagesbogen,
    siehe ``_account_start``); gibt es davon keinen, startet ab dem Zielmonat selbst.
    So ist der Saldo korrekt, egal in welcher Reihenfolge exportiert wird; bereits
    abgeschlossene Monate bleiben unangetastet (idempotent/Lock).
    """
    user = (await db.execute(select(User).where(User.id == employee_id))).scalar_one_or_none()
    if user is None:
        return

    target = date(year, month, 1)
    account_start = await _account_start(db, tenant_id, user)
    if account_start is None:
        start = target
    else:
        start = date(account_start.year, account_start.month, 1)
    if start > target:
        start = target

    y, m = start.year, start.month
    guard = 0
    while (y, m) <= (target.year, target.month) and guard < 600:
        await settle_month(db, tenant_id, user, y, m, actor_id)
        m += 1
        if m > 12:
            m = 1
            y += 1
        guard += 1


async def _delete_settlement_rows(
    db: AsyncSession, tenant_id: uuid.UUID, user: User, ym: date
) -> Decimal:
    """Abschluss-Zeilen eines Monats entfernen (accrual/payout/forfeit +
    UEBERSTUNDEN_AUSZAHLUNG-Lohnbuchung). Manuelle Korrekturen bleiben.

    Gibt die bisher gebuchte Auszahlungssumme zurueck (fuers Audit-Log).
    Gemeinsamer Unterbau von ``resettle_month`` (danach frisch abschliessen)
    und ``reopen_month`` (offen lassen — der laufende Monat rechnet live).
    """
    old_entries = list(
        (
            await db.execute(
                tenant_scoped(select(OvertimeEntry), OvertimeEntry, tenant_id).where(
                    OvertimeEntry.employee_id == user.id,
                    OvertimeEntry.year_month == ym,
                    OvertimeEntry.entry_type.in_(["accrual", "payout", "forfeit"]),
                )
            )
        )
        .scalars()
        .all()
    )
    alte_auszahlung = Decimal("0")
    for e in old_entries:
        if e.entry_type == "payout":
            alte_auszahlung += -Decimal(str(e.hours))
        await db.delete(e)
    alte_auszahlung = alte_auszahlung.quantize(PRAEZISION)
    await db.flush()

    old_bookings = list(
        (
            await db.execute(
                tenant_scoped(select(PayrollBooking), PayrollBooking, tenant_id).where(
                    PayrollBooking.employee_id == user.id,
                    PayrollBooking.year_month == ym,
                    PayrollBooking.wage_type_code == "UEBERSTUNDEN_AUSZAHLUNG",
                )
            )
        )
        .scalars()
        .all()
    )
    for b in old_bookings:
        await db.delete(b)
    await db.flush()
    return alte_auszahlung


async def reopen_month(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    user: User,
    year: int,
    month: int,
    actor_id: uuid.UUID | None = None,
) -> dict:
    """Einen versehentlich abgeschlossenen LAUFENDEN Monat wieder oeffnen.

    Loescht die Abschluss-Zeilen und schliesst NICHT neu ab — der laufende
    Monat rechnet danach wieder live (``live_balance``), der regulaere
    Abschluss uebernimmt ihn am Monatsende wie vorgesehen. Vergangene Monate
    gehoeren nicht hierher: dort ``resettle_month`` verwenden, sonst fehlt
    ihre Ansammlung dauerhaft im Saldo.
    """
    ym = date(year, month, 1)
    alte_auszahlung = await _delete_settlement_rows(db, tenant_id, user, ym)
    await audit_service.log(
        db,
        tenant_id=tenant_id,
        actor_id=actor_id,
        action="overtime.reopen",
        resource_type="user",
        resource_id=user.id,
        details={
            "year": year,
            "month": month,
            "alte_auszahlung": str(alte_auszahlung),
        },
    )
    await db.flush()
    return {"alte_auszahlung": alte_auszahlung}


async def resettle_month(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    user: User,
    year: int,
    month: int,
    actor_id: uuid.UUID | None = None,
) -> dict:
    """Monatsabschluss zurücksetzen und neu ausführen.

    ``settle_month`` sperrt einen Monat, sobald er einmal abgeschlossen ist —
    nachträgliche Korrekturen (z. B. ein Anfangsbestand im Vormonat) wirken
    sich dann nicht mehr aus. Dieser Ablauf hebt die Sperre für GENAU diesen
    Monat auf: die accrual-/payout-Zeilen sowie die zugehörige
    UEBERSTUNDEN_AUSZAHLUNG-Lohnbuchung werden gelöscht, danach läuft
    ``settle_month`` frisch. Manuelle Korrekturen (``adjustment``) werden
    NICHT angefasst — sie sind kein Bestandteil des Abschlusses.
    """
    ym = date(year, month, 1)
    alte_auszahlung = await _delete_settlement_rows(db, tenant_id, user, ym)

    await settle_month(db, tenant_id, user, year, month, actor_id)

    new_payout = (
        await db.execute(
            tenant_scoped(select(OvertimeEntry), OvertimeEntry, tenant_id).where(
                OvertimeEntry.employee_id == user.id,
                OvertimeEntry.year_month == ym,
                OvertimeEntry.entry_type == "payout",
            )
        )
    ).scalar_one_or_none()
    neue_auszahlung = (
        (-Decimal(str(new_payout.hours))).quantize(PRAEZISION) if new_payout else Decimal("0")
    )

    await audit_service.log(
        db,
        tenant_id=tenant_id,
        actor_id=actor_id,
        action="overtime.resettle",
        resource_type="user",
        resource_id=user.id,
        details={
            "year": year,
            "month": month,
            "alte_auszahlung": str(alte_auszahlung),
            "neue_auszahlung": str(neue_auszahlung),
        },
    )
    await db.flush()
    return {"alte_auszahlung": alte_auszahlung, "neue_auszahlung": neue_auszahlung}


async def settle_previous_month_all(db: AsyncSession, today: date | None = None) -> int:
    """Schliesst ALLE offenen Vormonate fuer aktive Mitarbeiter ab (idempotent).

    Laeuft taeglich im Hintergrund (main.py-Lifespan). Abgeschlossen wird bis
    einschliesslich Vormonat, nicht nur der unmittelbare Vormonat (Spec:
    liegen mehrere Monate offen, z. B. nach Ausfall des Loops, holt
    ``settle_through`` sie alle nach); dank Idempotenz in ``settle_month``
    passiert pro Mitarbeiter-Monat genau ein Abschluss, jeder echte Abschluss
    wird im Audit-Log festgehalten (Spec 2026-07-09).

    NUR Mitarbeiter in Tenants mit ``export_basis == "soll"`` (Einstellungen
    -> Zuschläge, ``tenant.settings["zuschlaege"]["export_basis"]``, Default
    "ist") werden automatisch abgeschlossen: im Ist-Modus bucht der Export die
    Ist-Stunden schon voll, ein zusaetzlicher above_cap/all-Abschluss würde
    per Bypass eine UEBERSTUNDEN_AUSZAHLUNG-Buchung in den Ist-Export
    durchlassen -> Doppelvergütung. Der Ist-Modus bleibt damit systemweit von
    diesem Loop unberührt. Tenants werden einmal pro Lauf geladen (nicht pro
    Mitarbeiter).
    """
    from zeiterfassung.models.tenant import Tenant

    today = today or date.today()
    prev_last = date(today.year, today.month, 1) - timedelta(days=1)
    year, month = prev_last.year, prev_last.month

    tenants_by_id = {t.id: t for t in (await db.execute(select(Tenant))).scalars().all()}

    users = (
        (await db.execute(select(User).where(User.is_active == True)))  # noqa: E712
        .scalars()
        .all()
    )
    settled = 0
    for user in users:
        tenant = tenants_by_id.get(user.tenant_id)
        zuschlaege = ((tenant.settings or {}).get("zuschlaege") or {}) if tenant else {}
        export_basis = str(zuschlaege.get("export_basis", "ist"))
        if export_basis != "soll":
            continue  # Ist-Modus: Auto-Abschluss würde den Ist-Export doppelt vergüten

        before = _settled_months(await _all_entries(db, user.tenant_id, user.id))
        await settle_through(db, user.tenant_id, user.id, year, month, None)
        after = _settled_months(await _all_entries(db, user.tenant_id, user.id))
        for ym in sorted(after - before):
            await audit_service.log(
                db,
                tenant_id=user.tenant_id,
                actor_id=None,
                action="overtime.settle",
                resource_type="user",
                resource_id=user.id,
                details={"year": ym.year, "month": ym.month, "mode": user.overtime_payout_mode},
            )
            settled += 1
    if settled:
        await db.commit()
    return settled


async def add_adjustment(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    employee_id: uuid.UUID,
    hours: Decimal,
    reason: str,
    effective_date: date,
    actor_id: uuid.UUID | None = None,
) -> OvertimeEntry:
    """Manuelle Korrektur / Anfangsbestand (±h) ins Log."""
    now = datetime.now(tz=UTC)
    entry = OvertimeEntry(
        tenant_id=tenant_id,
        employee_id=employee_id,
        year_month=date(effective_date.year, effective_date.month, 1),
        entry_date=effective_date,
        entry_type="adjustment",
        hours=Decimal(str(hours)).quantize(PRAEZISION),
        reason=reason,
        created_by=actor_id,
        created_at=now,
    )
    db.add(entry)
    await db.flush()
    return entry


async def _get_adjustment(
    db: AsyncSession, tenant_id: uuid.UUID, entry_id: uuid.UUID
) -> OvertimeEntry | None:
    """Nur manuelle Korrekturen (entry_type='adjustment') sind editierbar/löschbar;
    Ansammlungen/Auszahlungen aus dem Monatsabschluss bleiben unveränderlich."""
    return (
        await db.execute(
            tenant_scoped(select(OvertimeEntry), OvertimeEntry, tenant_id).where(
                OvertimeEntry.id == entry_id,
                OvertimeEntry.entry_type == "adjustment",
            )
        )
    ).scalar_one_or_none()


def _adjustment_snapshot(entry: OvertimeEntry) -> dict:
    """JSON-serialisierbarer Abzug eines Log-Eintrags für den Audit-Trail."""
    return {
        "hours": str(entry.hours),
        "reason": entry.reason,
        "entry_date": entry.entry_date.isoformat(),
        "year_month": entry.year_month.isoformat(),
    }


async def update_adjustment(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    entry_id: uuid.UUID,
    hours: Decimal,
    reason: str,
    effective_date: date,
    actor_id: uuid.UUID | None = None,
) -> OvertimeEntry | None:
    """Eine manuelle Korrektur bearbeiten. None, wenn nicht gefunden/keine Korrektur.

    Der Ledger ist eigentlich append-only (BetrVG §87: jede Mutation nachvollziehbar).
    Solange dieser Endpunkt eine In-Place-Bearbeitung anbietet, wird jede Änderung
    mit Vorher/Nachher-Stand im Audit-Log festgehalten.
    """
    entry = await _get_adjustment(db, tenant_id, entry_id)
    if entry is None:
        return None
    before = _adjustment_snapshot(entry)
    entry.hours = Decimal(str(hours)).quantize(PRAEZISION)
    entry.reason = reason
    entry.entry_date = effective_date
    entry.year_month = date(effective_date.year, effective_date.month, 1)
    await audit_service.log(
        db,
        tenant_id=tenant_id,
        actor_id=actor_id,
        action="overtime.adjustment.update",
        resource_type="overtime_ledger",
        resource_id=str(entry_id),
        details={"before": before, "after": _adjustment_snapshot(entry)},
    )
    await db.flush()
    return entry


async def delete_adjustment(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    entry_id: uuid.UUID,
    actor_id: uuid.UUID | None = None,
) -> bool:
    """Eine manuelle Korrektur löschen. False, wenn nicht gefunden/keine Korrektur.

    Das Löschen einer Ledger-Zeile wird vor der Entfernung mit vollständigem
    Snapshot im Audit-Log festgehalten (BetrVG §87 – Nachvollziehbarkeit).
    """
    entry = await _get_adjustment(db, tenant_id, entry_id)
    if entry is None:
        return False
    await audit_service.log(
        db,
        tenant_id=tenant_id,
        actor_id=actor_id,
        action="overtime.adjustment.delete",
        resource_type="overtime_ledger",
        resource_id=str(entry_id),
        details={"deleted": _adjustment_snapshot(entry)},
    )
    await db.delete(entry)
    await db.flush()
    return True


async def _month_exported(db: AsyncSession, tenant_id: uuid.UUID, ym: date) -> bool:
    """True, wenn für ``ym`` bereits ein (fertiger) Lohn-Export existiert.

    "Exportiert" heißt: es gibt für den Mandanten einen ``PayrollExport`` mit
    diesem ``year_month`` im Status GENERATED oder DELIVERED (nicht nur PREVIEW,
    der ändert nichts Verbindliches, und nicht FAILED).
    """
    from zeiterfassung.models.export import ExportStatus, PayrollExport

    hit = (
        await db.execute(
            tenant_scoped(select(PayrollExport.id), PayrollExport, tenant_id).where(
                PayrollExport.year_month == ym,
                PayrollExport.status.in_([ExportStatus.GENERATED, ExportStatus.DELIVERED]),
            )
        )
    ).first()
    return hit is not None


async def auto_resettle_if_needed(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    employee_id: uuid.UUID,
    affected_date: date,
    actor_id: uuid.UUID | None = None,
) -> str | None:
    """Automatischer Neuabschluss betroffener Monate nach einer rückwirkenden Änderung.

    Wird nach Überstunden-Korrekturen, Abwesenheits-Genehmigungen/-Widerrufen und
    Stempel-Korrekturen aufgerufen. Liegt ``affected_date`` in einem bereits per
    ``settle_month`` abgeschlossenen Monat, baut sich der Saldo der Folgemonate
    darauf auf — also wird nicht nur dieser Monat, sondern auch jeder bereits
    abgeschlossene FOLGEmonat bis einschließlich Vormonat per ``resettle_month``
    neu gerechnet (der aktuelle Monat rechnet ohnehin live, siehe ``live_balance``).

    Export-Sperre: Sobald für einen Monat bereits ein ``PayrollExport``
    existiert (Status GENERATED/DELIVERED), wird dieser Monat NICHT mehr
    automatisch neu abgeschlossen — sonst liefen Lohn-Export und Konto
    auseinander. Die Kaskade bricht dann an genau dieser Stelle ab.

    Rückgabe:
      - ``"resettled"``: mindestens ein Monat wurde neu abgeschlossen.
      - ``"exported"``: Abbruch, weil (mindestens) ein betroffener Monat bereits
        exportiert ist — der Aufrufer soll den Nutzer darauf hinweisen.
      - ``None``: nichts zu tun (Monat noch offen/aktuell) oder ein Fehler trat
        auf (wird geloggt, aber NIE an den aufrufenden Schreibpfad durchgereicht).
    """
    try:
        today = datetime.now(tz=UTC).date()
        start_ym = date(affected_date.year, affected_date.month, 1)
        current_ym = date(today.year, today.month, 1)
        if start_ym >= current_ym:
            return None  # laufender/zukünftiger Monat -> der normale Lauf erledigt das

        if not await _is_month_settled(db, tenant_id, employee_id, start_ym):
            return None  # betroffener Monat war noch offen -> nichts zu tun

        user = await get_scoped(db, User, tenant_id, employee_id)
        if user is None:
            return None

        prev_y, prev_m = today.year, today.month - 1
        if prev_m == 0:
            prev_m, prev_y = 12, prev_y - 1
        last_ym = date(prev_y, prev_m, 1)

        y, m = start_ym.year, start_ym.month
        resettled_any = False
        guard = 0
        while (y, m) <= (last_ym.year, last_ym.month) and guard < 600:
            ym = date(y, m, 1)
            if await _is_month_settled(db, tenant_id, employee_id, ym):
                if await _month_exported(db, tenant_id, ym):
                    logging.warning(
                        "auto_resettle_if_needed: Monat %s (Mitarbeiter %s) bereits "
                        "exportiert -> kein automatischer Neuabschluss",
                        ym,
                        employee_id,
                    )
                    return "exported"
                await resettle_month(db, tenant_id, user, y, m, actor_id)
                resettled_any = True
            m += 1
            if m > 12:
                m, y = 1, y + 1
            guard += 1

        return "resettled" if resettled_any else None
    except Exception:
        logging.warning(
            "auto_resettle_if_needed fehlgeschlagen (tenant=%s, employee=%s, date=%s)",
            tenant_id,
            employee_id,
            affected_date,
            exc_info=True,
        )
        return None


# ---------------------------------------------------------------------------
# Router-facing helpers (moved verbatim out of api/v1/users.py, zeiterfassung-6pcv)
# ---------------------------------------------------------------------------


async def balance_for_employee(
    db: AsyncSession, tenant_id: uuid.UUID, employee_id: uuid.UUID
) -> dict:
    """Aktueller Überstunden-Saldo + Auszahl-Modus eines Mitarbeiters.

    Moved verbatim from ``GET /overtime/balance``.
    """
    u = await get_scoped(db, User, tenant_id, employee_id)
    # Laufender Ist-Saldo seit Eintritt (täglich aufsummiert, Korrekturen inklusive).
    bal = (
        await live_balance(db, tenant_id, u)
        if u is not None
        else await current_balance(db, tenant_id, employee_id)
    )
    return {
        "employee_id": str(employee_id),
        "balance_hours": float(bal),
        "payout_mode": u.overtime_payout_mode if u else None,
        "cap_hours": (
            float(u.overtime_cap_hours) if (u and u.overtime_cap_hours is not None) else None
        ),
    }


async def ledger_for_employee(
    db: AsyncSession, tenant_id: uuid.UUID, employee_id: uuid.UUID
) -> list[dict]:
    """Überstunden-Log eines Mitarbeiters (chronologisch).

    Moved verbatim from ``GET /overtime/ledger``.
    """
    rows = (
        (
            await db.execute(
                tenant_scoped(select(OvertimeEntry), OvertimeEntry, tenant_id)
                .where(
                    OvertimeEntry.employee_id == employee_id,
                )
                .order_by(OvertimeEntry.year_month, OvertimeEntry.entry_date)
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": str(r.id),
            "year_month": r.year_month.isoformat(),
            "entry_date": r.entry_date.isoformat(),
            "entry_type": r.entry_type,
            "hours": float(r.hours),
            "reason": r.reason,
        }
        for r in rows
    ]


async def _load_employee_or_404(
    db: AsyncSession, tenant_id: uuid.UUID, employee_id: uuid.UUID
) -> User:
    from fastapi import HTTPException, status

    user = await get_scoped(db, User, tenant_id, employee_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")
    return user


async def daily_for_employee(
    db: AsyncSession, tenant_id: uuid.UUID, employee_id: uuid.UUID
) -> list[dict]:
    """Tages-Verlauf des Arbeitszeitkontos ab Stichtag.

    Moved verbatim from ``GET /overtime/daily``.
    """
    user = await _load_employee_or_404(db, tenant_id, employee_id)
    return await daily_breakdown(db, tenant_id, user)


async def month_summary_for_employee(
    db: AsyncSession, tenant_id: uuid.UUID, employee_id: uuid.UUID, year: int, month: int
) -> dict:
    """Monats-Summen fürs Kalender-Kopfzeilen-Trio: Ist, effektives Soll, Saldo.

    Moved verbatim from ``GET /overtime/month-summary``.
    """
    user = await _load_employee_or_404(db, tenant_id, employee_id)
    return await month_summary(db, tenant_id, user, year, month)


async def create_adjustment(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    body: dict,
    *,
    actor_employee_id: uuid.UUID | None,
) -> dict:
    """Manuelle Überstunden-Korrektur / Anfangsbestand (±h) ins Log.

    Moved verbatim from ``POST /overtime/adjustment``.
    """
    from fastapi import HTTPException, status

    emp = body.get("employee_id")
    if not emp:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="employee_id erforderlich"
        )
    try:
        hours = Decimal(str(body.get("hours")).replace(",", "."))
    except (InvalidOperation, TypeError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="hours muss eine Zahl sein"
        ) from None
    reason = (body.get("reason") or "Manuelle Korrektur").strip()
    eff = date.fromisoformat(str(body["date"])[:10]) if body.get("date") else date.today()
    # Ziel-Mitarbeiter muss zum eigenen Mandanten gehoeren.
    ok = (
        await db.execute(
            tenant_scoped(select(User.id), User, tenant_id).where(User.id == uuid.UUID(str(emp)))
        )
    ).first()
    if ok is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Mitarbeiter nicht gefunden"
        )
    await add_adjustment(
        db,
        tenant_id,
        uuid.UUID(str(emp)),
        hours,
        reason,
        eff,
        actor_employee_id,
    )
    result = await auto_resettle_if_needed(
        db, tenant_id, uuid.UUID(str(emp)), eff, actor_employee_id
    )
    await db.commit()
    response: dict = {"ok": True}
    if result == "exported":
        response["hinweis"] = (
            "Monat bereits exportiert — Abschluss nicht automatisch neu berechnet. "
            "In den Einstellungen Monat neu abschliessen und Export neu erzeugen."
        )
    return response


async def update_adjustment_for_router(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    entry_id: uuid.UUID,
    body: dict,
    *,
    actor_employee_id: uuid.UUID | None,
) -> dict:
    """Eine manuelle Überstunden-Korrektur bearbeiten (nur adjustment-Einträge).

    Moved verbatim from ``PATCH /overtime/adjustment/{entry_id}``.
    """
    from fastapi import HTTPException, status

    try:
        hours = Decimal(str(body.get("hours")).replace(",", "."))
    except (InvalidOperation, TypeError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="hours muss eine Zahl sein"
        ) from None
    reason = (body.get("reason") or "Manuelle Korrektur").strip()
    eff = date.fromisoformat(str(body["date"])[:10]) if body.get("date") else date.today()
    updated = await update_adjustment(
        db, tenant_id, entry_id, hours, reason, eff, actor_id=actor_employee_id
    )
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Korrektur nicht gefunden"
        )
    result = await auto_resettle_if_needed(
        db, tenant_id, updated.employee_id, eff, actor_employee_id
    )
    await db.commit()
    response: dict = {"ok": True}
    if result == "exported":
        response["hinweis"] = (
            "Monat bereits exportiert — Abschluss nicht automatisch neu berechnet. "
            "In den Einstellungen Monat neu abschliessen und Export neu erzeugen."
        )
    return response


async def delete_adjustment_for_router(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    entry_id: uuid.UUID,
    *,
    actor_employee_id: uuid.UUID | None,
) -> None:
    """Eine manuelle Überstunden-Korrektur löschen (nur adjustment-Einträge).

    Moved verbatim from ``DELETE /overtime/adjustment/{entry_id}``.
    """
    from fastapi import HTTPException, status

    ok = await delete_adjustment(db, tenant_id, entry_id, actor_id=actor_employee_id)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Korrektur nicht gefunden"
        )
    await db.commit()
