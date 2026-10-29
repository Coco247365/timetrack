"""DATEV LODAS payroll export service.

Builds LODAS Bewegungsdaten from MonthlySummary + ApprovalRequest data,
using a real-world Lohnart mapping (BeraterNr 12345).

Reference: docs/datev-lodas-analysis.md
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from zeiterfassung.core.datev import (
    DatevRow,
    LodasConfig,
    compute_checksum,
    preview_datev_export,
    write_lodas_export,
)
from zeiterfassung.core.errors import DomainError, ErrorCode
from zeiterfassung.models.absence import ApprovalRequest, RequestStatus, RequestType
from zeiterfassung.models.export import ExportStatus, PayrollExport, WageTypeMapping
from zeiterfassung.models.user import User
from zeiterfassung.policy.overtime import (
    hat_tagessoll,
    weekly_target_minutes,
)
from zeiterfassung.policy.tagesregeln import Tagesregeln
from zeiterfassung.schemas.export import DatevExportRequest, DatevPreviewResponse
from zeiterfassung.services import audit_service

_elog = logging.getLogger(__name__)


class ExportError(DomainError):
    code = ErrorCode.EXPORT_NOT_FOUND


async def _get_wage_type_mappings(db: AsyncSession, tenant_id: uuid.UUID) -> dict[str, int]:
    """Code -> DATEV-Nummer aufloesen (Katalog + Kategorie-Zuordnung).

    - Katalog-Zeilen (LA_xxxx aus dem Steuerberater-PDF): direkt ihre Nummer —
      damit sind manuelle Buchungen auf konkrete Lohnarten exportierbar.
    - System-Kategorien (NORMALE_ARBEITSZEIT, SAMSTAG_ARBEIT, ...): ueber die in
      den Einstellungen gepflegte Zuordnung (tenant.settings["lohnart_zuordnung"]).
    Ohne Zuordnung/Nummer wird ein Code schlicht nicht exportiert. Keine
    hartkodierten Nummern im Code.
    """
    result = await db.execute(
        select(WageTypeMapping).where(
            WageTypeMapping.tenant_id == tenant_id,
            WageTypeMapping.is_active == True,  # noqa: E712
            WageTypeMapping.datev_lohnart.is_not(None),
        )
    )
    mappings = {m.internal_code: m.datev_lohnart for m in result.scalars().all()}

    # Kategorie-Zuordnung aufloesen: CATEGORY -> internal_code -> Nummer.
    from zeiterfassung.models.tenant import Tenant

    tenant = (await db.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one_or_none()
    zuordnung = ((tenant.settings or {}).get("lohnart_zuordnung") or {}) if tenant else {}
    for category, code in zuordnung.items():
        if code in mappings:
            mappings[category] = mappings[code]
    return mappings


#: AU-Status einer Krankmeldung -> spezifische Export-Kategorie. Kranktage ohne
#: Status (oder ohne zugeordnete Lohnart fuer die spezifische Kategorie) fallen
#: auf die Sammel-Kategorie KRANK_STD zurueck.
SICK_STATUS_CATEGORY: dict[str, str] = {
    "EAU_TELEFON": "KRANK_EAU_TELEFON",
    "AU_MIT_LFZ": "KRANK_MIT_AU",
    "OHNE_AU_MIT_LFZ": "KRANK_OHNE_AU_MIT_LFZ",
    "OHNE_AU_OHNE_LFZ": "KRANK_OHNE_AU_OHNE_LFZ",
}


def resolve_employee_lohnart(
    wage_types: dict[str, int],
    profiles: dict[str, dict],
    profile_id: str | None,
    internal_code: str,
) -> int | None:
    """DATEV-Nummer fuer einen Code aufloesen — Profil-Abweichung zuerst.

    Reihenfolge: Abrechnungsprofil des Mitarbeiters (Kategorie -> LA-Code ->
    Nummer), sonst die Standard-Aufloesung aus ``wage_types``. None = nicht
    exportieren.
    """
    if profile_id:
        profile = profiles.get(profile_id)
        if profile:
            override_code = (profile.get("mapping") or {}).get(internal_code)
            if override_code and override_code in wage_types:
                return wage_types[override_code]
    return wage_types.get(internal_code)


def resolve_employee_earliest_start(
    profiles: dict[str, dict], profile_id: str | None
) -> int | None:
    """Profil-Regel ``fruehester_beginn`` fuer einen Mitarbeiter aufloesen.

    Kein Profil/Standard-Profil (kein eigenes ``fruehester_beginn`` gepflegt)
    -> None (keine Kappung).
    """
    from zeiterfassung.payroll.rules import parse_earliest_start

    if not profile_id:
        return None
    profile = profiles.get(profile_id)
    if not profile:
        return None
    return parse_earliest_start(profile.get("fruehester_beginn"))


def resolve_employee_mehrarbeit_frei_min(
    profiles: dict[str, dict], profile_id: str | None, default_min: int
) -> int:
    """Profil-Override ``mehrarbeit_frei_wochen_std`` (Std/Woche) fuer einen
    Mitarbeiter aufloesen; ohne Override gilt der Mandanten-Wert.
    """
    if not profile_id:
        return default_min
    profile = profiles.get(profile_id)
    if not profile:
        return default_min
    val = profile.get("mehrarbeit_frei_wochen_std")
    if val is None:
        return default_min
    return int(round(float(val) * 60))


def resolve_employee_weekend_counts(profiles: dict[str, dict], profile_id: str | None) -> bool:
    """Profil-Regel ``wochenende_ins_konto``: zaehlt Wochenend-/Feiertags-Ist
    in die Mehrarbeit/das Konto? Default (kein Profil/keine Regel) = False —
    dann laeuft WE-Arbeit nur ueber die Zuschlags-Lohnarten (#21, Beleg PN5
    Mai 2026: Samstag ausschliesslich auf 015, nicht in 090).
    """
    if not profile_id:
        return False
    profile = profiles.get(profile_id)
    if not profile:
        return False
    return bool(profile.get("wochenende_ins_konto", False))


def _month_range(year: int, month: int) -> tuple[date, date]:
    """Return (first_day, last_day) of a month."""
    first = date(year, month, 1)
    if month == 12:
        last = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last = date(year, month + 1, 1) - timedelta(days=1)
    return first, last


async def _target_employee_ids(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    year: int,
    month: int,
    employee_ids: list[uuid.UUID] | None,
) -> list[uuid.UUID]:
    """Mitarbeiter für den Export: explizite Auswahl oder alle mit Daten im Monat."""
    from zeiterfassung.models.clock import DailyTimesheet

    if employee_ids:
        return list(dict.fromkeys(employee_ids))

    year_month = date(year, month, 1)
    _, month_end = _month_range(year, month)
    ts_ids = (
        (
            await db.execute(
                select(DailyTimesheet.employee_id)
                .where(
                    DailyTimesheet.tenant_id == tenant_id,
                    DailyTimesheet.work_date >= year_month,
                    DailyTimesheet.work_date <= month_end,
                )
                .distinct()
            )
        )
        .scalars()
        .all()
    )
    abs_ids = (
        (
            await db.execute(
                select(ApprovalRequest.employee_id)
                .where(
                    ApprovalRequest.tenant_id == tenant_id,
                    ApprovalRequest.status == RequestStatus.APPROVED,
                    ApprovalRequest.date_from <= month_end,
                    ApprovalRequest.date_to >= year_month,
                    ApprovalRequest.request_type.in_(
                        [RequestType.VACATION, RequestType.SICK_LEAVE]
                    ),
                )
                .distinct()
            )
        )
        .scalars()
        .all()
    )
    return list({*ts_ids, *abs_ids})


async def _ensure_monthly_summaries(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    ids: list[uuid.UUID],
    year: int,
    month: int,
) -> None:
    """Monatssummen aus den Tagesdaten frisch aggregieren (vor dem Export)."""
    from zeiterfassung.tasks.timesheet_tasks import aggregate_monthly_summary

    for eid in ids:
        await aggregate_monthly_summary(db, tenant_id, eid, year, month)


async def _build_datev_rows(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    year: int,
    month: int,
    employee_ids: list[uuid.UUID] | None,
    wage_types: dict[str, int],
    employment_type: str | None = None,
) -> list[DatevRow]:
    """Build LODAS rows from MonthlySummary + absence data.

    ``employment_type``: 'hourly' = nur Lohn-Empfänger, 'salaried' = nur
    Gehaltsempfänger, None = alle. Gilt für Zeit-Zeilen UND manuelle
    Lohnbuchungen.
    """
    year_month = date(year, month, 1)
    month_start, month_end = _month_range(year, month)

    # #71: Beginnt der Monat nicht montags, wird das Wochen-Freikontingent
    # der angebrochenen ISO-Woche mit den Vormonats-Tagen vorbelegt.
    week_lead_start = month_start - timedelta(days=month_start.weekday())
    lead_days: list[date] = []
    _dd = week_lead_start
    while _dd < month_start:
        lead_days.append(_dd)
        _dd += timedelta(days=1)

    # Zielmitarbeiter bestimmen (explizite Auswahl oder alle mit Daten im Monat).
    target_ids = await _target_employee_ids(db, tenant_id, year, month, employee_ids)

    # Mitarbeiterkreis-Filter (Lohn vs. Gehalt): Gehaltsempfaenger laufen fix
    # ueber ihre Gehalts-Lohnart und sollen standardmaessig NICHT als Stunden
    # exportiert werden.
    allowed_by_type: set[uuid.UUID] | None = None
    if employment_type:
        # "Lohn" = nach Stunden bezahlt: Stundenlohn UND Werkstudenten (die
        # bekommen ihre Ist-Stunden als Zeitlohn-Zeile, s. Werkstudenten-Zweig).
        typen = ["hourly", "werkstudent"] if employment_type == "hourly" else [employment_type]
        allowed_by_type = set(
            (
                await db.execute(
                    select(User.id).where(
                        User.tenant_id == tenant_id,
                        User.employment_type.in_(typen),
                    )
                )
            )
            .scalars()
            .all()
        )
        target_ids = [tid_ for tid_ in target_ids if tid_ in allowed_by_type]

    # Zeitbasierte Zeilen (Brutto/Zuschläge) stammen aus MonthlySummary -> aggregieren.
    await _ensure_monthly_summaries(db, tenant_id, target_ids, year, month)

    # Überstunden: KEIN Monatsabschluss/keine Auszahlung mehr. Der Arbeitszeitsaldo
    # wird laufend tagesgenau aus den Tagesbögen berechnet (Ist−Soll je Tag); die
    # monatliche Auszahlung kommt später separat. (settle_through bleibt für später.)

    # ── Zeit-Zeilen aus der Regel-Schicht (payroll/rules) ────────────────────
    # Kategorien (Normal / Samstag / Sonntag / Feiertag / Nacht / bezahlte
    # Feiertage / Urlaub / Krank) werden minutengenau aus den Stempel-Events
    # abgeleitet. Exportiert wird NUR, was im Katalog eine DATEV-Nummer hat.
    from zoneinfo import ZoneInfo

    from zeiterfassung.core.holidays import is_holiday, working_days_between
    from zeiterfassung.models.clock import PunchEvent
    from zeiterfassung.models.tenant import Tenant
    from zeiterfassung.payroll.rules import (
        SurchargeConfig,
        absence_minutes,
        categorize_day,
        clip_segments_local_tz,
        clip_segments_window_local_tz,
        paid_holiday_minutes,
        segments_from_events,
    )
    from zeiterfassung.services.tenant_service import resolve_bundesland

    rows: list[DatevRow] = []
    bundesland = await resolve_bundesland(db, tenant_id)
    tzinfo = ZoneInfo("Europe/Berlin")

    # Abrechnungsprofile (Einstellungen): pro Mitarbeiter abweichende Lohnarten.
    _tenant_row = (
        await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    ).scalar_one_or_none()
    profiles: dict[str, dict] = {
        str(p.get("id")): p
        for p in (((_tenant_row.settings or {}).get("wage_profiles") or []) if _tenant_row else [])
    }
    profile_by_employee: dict[uuid.UUID, str | None] = {}

    # Folgelohnarten (Katalog): Buchung auf X erzeugt automatisch eine zweite
    # Zeile mit gleichem Wert auf Y — wie im echten Referenz-Export (129 Urlaublohn
    # -> 150 zus. Urlaubsgeld, 066 Überstunden -> 067 Zuschlag 30%).
    _catalog_rows = (
        (
            await db.execute(
                select(WageTypeMapping).where(
                    WageTypeMapping.tenant_id == tenant_id,
                    WageTypeMapping.is_active == True,  # noqa: E712
                    WageTypeMapping.datev_lohnart.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )
    _number_by_code = {r.internal_code: r.datev_lohnart for r in _catalog_rows}
    folge_by_number: dict[int, int] = {}
    for r in _catalog_rows:
        if r.folge_lohnart and r.folge_lohnart in _number_by_code:
            folge_by_number[r.datev_lohnart] = _number_by_code[r.folge_lohnart]

    def _add(
        pnr: str, internal_code: str, minutes: int, employee_id: uuid.UUID | None = None
    ) -> None:
        if minutes <= 0:
            return
        lohnart = resolve_employee_lohnart(
            wage_types, profiles, profile_by_employee.get(employee_id), internal_code
        )
        if lohnart is None:
            return
        wert = round(minutes / 60.0, 2)
        rows.append(
            DatevRow(
                personalnummer=pnr,
                lohnart=lohnart,
                wert=wert,
                abrechnungszeitraum=year_month,
            )
        )
        folge = folge_by_number.get(lohnart)
        if folge is not None:
            rows.append(
                DatevRow(
                    personalnummer=pnr,
                    lohnart=folge,
                    wert=wert,
                    abrechnungszeitraum=year_month,
                )
            )

    skipped_no_pnr: list[str] = []
    if target_ids:
        users = {
            u.id: u
            for u in (
                await db.execute(
                    select(User).where(User.tenant_id == tenant_id, User.id.in_(target_ids))
                )
            )
            .scalars()
            .all()
        }
        for _uid, _u in users.items():
            profile_by_employee[_uid] = _u.wage_profile_id

        # Arbeitszeit-Profile (kind 'nfc', Spec 2026-08-14): ein aktives Profil
        # kappt die Stempel mit seinem Fenster (Beginn UND Ende, ggf. ueber
        # Mitternacht); Mitarbeiter ohne (aktives) NFC-Profil fallen auf die
        # Lohnprofil-Regel "fruehester_beginn" zurueck (nur Beginn, wie bisher).
        from zeiterfassung.models.work_time_profile import WorkTimeProfile
        from zeiterfassung.policy import worktime

        _wt_profiles = {
            p.id: p
            for p in (
                await db.execute(
                    select(WorkTimeProfile).where(WorkTimeProfile.tenant_id == tenant_id)
                )
            )
            .scalars()
            .all()
        }
        window_by_employee: dict[uuid.UUID, worktime.Window] = {}
        for _uid, _u in users.items():
            _win = (
                worktime.nfc_window(_wt_profiles.get(_u.work_time_profile_id))
                if _u.work_time_profile_id
                else None
            )
            if _win is not None:
                window_by_employee[_uid] = _win

        # Zuschlags-Fenster aus den Mandanten-Einstellungen (Einstellungen-UI).
        tenant = (
            await db.execute(select(Tenant).where(Tenant.id == tenant_id))
        ).scalar_one_or_none()
        z = ((tenant.settings or {}).get("zuschlaege") or {}) if tenant else {}
        cfg = SurchargeConfig(
            night_start=int(z.get("night_start", 23)),
            night_end=int(z.get("night_end", 6)),
            saturday_from=int(z.get("saturday_from", 0)),
            sunday_from=int(z.get("sunday_from", 0)),
        )
        # Export-Basis (Einstellungen -> Zuschläge):
        # - "ist" (bisher): gearbeitete Stunden -> NORMALE_ARBEITSZEIT.
        # - "soll" (LODAS-kompatibel wie das Altsystem): Zeitlohn = effektive
        #   SOLLZEIT; Differenzen laufen als Mehrarbeit (Wochen-Staffel) bzw.
        #   Minusstunden. Siehe docs/datev-lodas-analysis.md.
        export_basis = str(z.get("export_basis", "ist"))
        mehrarbeit_frei_min = int(round(float(z.get("mehrarbeit_frei_wochen_std", 1.5)) * 60))

        # Events des Monats (±1 Tag Rand für lokale Tageszuordnung), Korrekturen gefiltert.
        win_start = datetime(
            week_lead_start.year, week_lead_start.month, week_lead_start.day, tzinfo=UTC
        ) - timedelta(days=1)
        win_end = datetime(
            month_end.year, month_end.month, month_end.day, 23, 59, 59, tzinfo=UTC
        ) + timedelta(days=1)
        superseded = set(
            (
                await db.execute(
                    select(PunchEvent.corrects_event_id).where(
                        PunchEvent.tenant_id == tenant_id,
                        PunchEvent.corrects_event_id.is_not(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        ev_rows = (
            (
                await db.execute(
                    select(PunchEvent)
                    .where(
                        PunchEvent.tenant_id == tenant_id,
                        PunchEvent.employee_id.in_(target_ids),
                        PunchEvent.event_time >= win_start,
                        PunchEvent.event_time <= win_end,
                        PunchEvent.is_void.is_(False),
                    )
                    .order_by(PunchEvent.event_time, PunchEvent.id)
                )
            )
            .scalars()
            .all()
        )
        events_by_emp: dict[uuid.UUID, list[tuple[str, datetime]]] = {}
        for ev in ev_rows:
            if ev.id in superseded:
                continue
            et = ev.event_time
            if et.tzinfo is not None:
                et = et.astimezone(UTC).replace(tzinfo=None)
            etype = ev.event_type.value if hasattr(ev.event_type, "value") else str(ev.event_type)
            events_by_emp.setdefault(ev.employee_id, []).append((etype, et))

        # EINE Quelle fuer das Ist: der Tagesbogen (clock_service rechnet ihn
        # aus den Stempeln — inkl. ArbZG-Mindestpause, Stempel-Kappung,
        # "Ausstempeln vergessen" = 0 h, manuell gesetzte/Auto-Tage). Zettel
        # und Konto lesen genau diesen Wert; der Export darf nicht mehr
        # parallel aus den Roh-Segmenten rechnen, sonst zahlt er z. B. die
        # automatisch angehobene Pause als Mehrarbeit aus (ein Mitarbeiter 08/2026:
        # +10:15 h Differenz zum Zettel). Ohne Bogen-Zeile (nur in Tests
        # denkbar, jeder Stempel legt sie an) bleibt die Segment-Rechnung.
        from zeiterfassung.models.clock import DailyTimesheet

        bogen_net_by_emp: dict[uuid.UUID, dict[date, int]] = {}
        for _eid, _wd, _net in (
            await db.execute(
                select(
                    DailyTimesheet.employee_id,
                    DailyTimesheet.work_date,
                    DailyTimesheet.net_work_minutes,
                ).where(
                    DailyTimesheet.tenant_id == tenant_id,
                    DailyTimesheet.employee_id.in_(target_ids),
                    DailyTimesheet.work_date >= week_lead_start,
                    DailyTimesheet.work_date <= month_end,
                )
            )
        ).all():
            bogen_net_by_emp.setdefault(_eid, {})[_wd] = int(_net or 0)

        # Genehmigte Abwesenheiten — auf den Monat geclippt (Halbtag zählt 0,5).
        absences = (
            (
                await db.execute(
                    select(ApprovalRequest).where(
                        ApprovalRequest.tenant_id == tenant_id,
                        ApprovalRequest.employee_id.in_(target_ids),
                        ApprovalRequest.status == RequestStatus.APPROVED,
                        ApprovalRequest.date_from <= month_end,
                        ApprovalRequest.date_to >= week_lead_start,
                        ApprovalRequest.request_type.in_(
                            [
                                RequestType.VACATION,
                                RequestType.SICK_LEAVE,
                                RequestType.UNPAID_LEAVE,
                                RequestType.SPECIAL_LEAVE,
                            ]
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        abs_days: dict[uuid.UUID, dict[str, float]] = {}
        # Kranktage getrennt nach AU-Status (None = kein Status erfasst).
        sick_by_status: dict[uuid.UUID, dict[str | None, float]] = {}
        absent_dates: dict[uuid.UUID, set[date]] = {}
        # Fuer den Soll-Modus: Abwesenheit tagesgenau (Datum, Anteil, Kategorie-
        # Schluessel) — Stunden = Tagessoll des jeweiligen Wochentags x Anteil.
        absence_day_entries: dict[uuid.UUID, list[tuple[date, float, str, str | None]]] = {}
        for ab in absences:
            frm = max(ab.date_from, week_lead_start)
            to = min(ab.date_to, month_end)
            frac = Tagesregeln.abwesenheits_deckung(ab)
            status_key = str(ab.sick_status) if ab.sick_status else None
            if ab.request_type == RequestType.VACATION:
                kind = "vacation"
            elif ab.request_type == RequestType.SICK_LEAVE:
                kind = "sick"
            elif ab.request_type == RequestType.SPECIAL_LEAVE:
                kind = "special"
            else:
                kind = "unpaid"
            frm_month = max(ab.date_from, month_start)
            days = (
                working_days_between(frm_month, to, bundesland) * frac if frm_month <= to else 0.0
            )
            if kind == "vacation":
                emp_days = abs_days.setdefault(ab.employee_id, {"vacation": 0.0})
                emp_days["vacation"] += days
            elif kind == "sick":
                per_status = sick_by_status.setdefault(ab.employee_id, {})
                per_status[status_key] = per_status.get(status_key, 0.0) + days
            dset = absent_dates.setdefault(ab.employee_id, set())
            entries = absence_day_entries.setdefault(ab.employee_id, [])
            dd = frm
            while dd <= to:
                holiday = is_holiday(dd, bundesland)
                # Feiertag: bei unbezahlter Abwesenheit geblockt+unverguetet;
                # im KRANK-Zeitraum als Kranklohn verguetet (Altsystem-Beleg
                # PN52 Mai 2026: 153=80,00 inkl. Pfingstmontag, 110 ohne ihn);
                # nur bei Urlaub/Sonderurlaub bleibt er Feiertagslohn (#69).
                if holiday:
                    if kind == "unpaid":
                        dset.add(dd)
                    elif kind == "sick":
                        dset.add(dd)
                        if dd.weekday() < 5:
                            entries.append((dd, frac, kind, status_key))
                else:
                    dset.add(dd)
                    if dd.weekday() < 5:
                        entries.append((dd, frac, kind, status_key))
                dd += timedelta(days=1)

        # Kalendertage + Feiertage des Monats.
        month_days: list[date] = []
        dd = month_start
        while dd <= month_end:
            month_days.append(dd)
            dd += timedelta(days=1)
        month_holidays = [d for d in month_days if is_holiday(d, bundesland)]

        for uid, user in users.items():
            # Ohne Personalnummer keine Zeile: DATEV kann interne IDs nicht
            # zuordnen (frueher landete ein UUID-Fragment in der Datei).
            if not (user.employee_number or "").strip():
                skipped_no_pnr.append(f"{user.first_name} {user.last_name}")
                continue
            # Eintritt erst nach dem Exportmonat -> gehoert nicht in die Datei
            # (Auto-Erfassung kann sonst Scheinstunden erzeugt haben).
            if user.hire_date is not None and user.hire_date > month_end:
                continue
            # Geloeschte (Soft-Delete/Testkonten) NIE exportieren. Ausgetretene
            # (archived) nur bis zu ihrem letzten Monat — der Schlussmonat
            # gehoert noch in die Abrechnung, danach nichts mehr.
            if user.employment_status == "deleted":
                continue
            # Werkstudenten/Aushilfen (Entscheidung 09.09.2026): kein Soll,
            # kein Konto, keine Staffel — in den Export kommt genau EINE Zeile
            # Zeitlohn mit den gearbeiteten Stunden des Monats (Tagesbogen-
            # Netto, wie auf dem Stundenzettel). Keine Abwesenheits-, Mehr-
            # oder Minusstunden-Zeilen.
            if not hat_tagessoll(user.employment_type):
                if not (user.employee_number or "").strip():
                    skipped_no_pnr.append(f"{user.first_name} {user.last_name}")
                    continue
                ist_gesamt = sum(bogen_net_by_emp.get(uid, {}).get(day, 0) for day in month_days)
                _add(user.employee_number, "NORMALE_ARBEITSZEIT", ist_gesamt, uid)
                continue
            if user.termination_date is not None and user.termination_date < month_start:
                continue
            pnr = user.employee_number
            # Abwesenheits-Stunden (Urlaub/Krank/Feiertag) = Tage × Tagessatz. Bei
            # wochentagsspezifischem Plan der Wochenschnitt (Wochen-Soll÷5); die
            # tagesgenaue Überstunden-Rechnung läuft separat im overtime_service.
            daily_target = round(
                weekly_target_minutes(user.contracted_hours_per_week, user.weekday_target_minutes)
                / 5.0
            )

            # Tages-Soll: EINE Stelle fuer alle Konsumenten (policy.tagesregeln).
            # Die fruehere _tagessoll-Kopie hier kannte weder Arbeitszeit-
            # Profile noch employment_type (Spec 25.08.).
            _tagessoll = Tagesregeln.fuer(
                user, _wt_profiles.get(user.work_time_profile_id)
            ).tagessoll

            segments = segments_from_events(events_by_emp.get(uid, []))
            # Stempel-Kappung: aktives NFC-Arbeitszeit-Profil (Fenster mit
            # Beginn UND Ende) vor der Lohnprofil-Regel "fruehester_beginn"
            # (nur Beginn, wie Altsystem "06:00 (05:19)"). Original-Events bleiben
            # unangetastet, nur diese Bewertung nutzt die gekappten Segmente
            # (Soll- UND Ist-Modus) — dieselbe Kette wie Konto und Zettel.
            window = window_by_employee.get(uid)
            if window is not None:
                segments = clip_segments_window_local_tz(segments, window, tzinfo)
            else:
                earliest_min = resolve_employee_earliest_start(
                    profiles, profile_by_employee.get(uid)
                )
                if earliest_min is not None:
                    segments = clip_segments_local_tz(segments, earliest_min, tzinfo)

            totals: dict[str, int] = {}
            ist_by_day: dict[date, int] = {}
            night_by_day: dict[date, int] = {}
            worked_dates: set[date] = set()
            for day in lead_days + month_days:
                off = int(
                    datetime(day.year, day.month, day.day, 12, tzinfo=tzinfo)
                    .utcoffset()
                    .total_seconds()
                    // 60
                )
                mins = categorize_day(
                    day,
                    segments,
                    is_holiday=is_holiday(day, bundesland),
                    cfg=cfg,
                    tz_offset_min=off,
                )
                ist_by_day[day] = sum(v for k, v in mins.items() if k != "NACHT_ARBEIT")
                night_by_day[day] = mins.get("NACHT_ARBEIT", 0)
                if any(v > 0 for k, v in mins.items() if k != "NACHT_ARBEIT"):
                    worked_dates.add(day)
                # Tagesbogen gewinnt (eine Quelle, s. o.): gewertetes Ist des Tages.
                bogen_net = bogen_net_by_emp.get(uid, {}).get(day)
                if bogen_net is not None:
                    ist_by_day[day] = bogen_net
                    if bogen_net > 0:
                        worked_dates.add(day)
                if day < month_start:
                    continue  # Vorlauf: nur ist/night fuer das Kontingent-Seeding
                for k, v in mins.items():
                    totals[k] = totals.get(k, 0) + v

            # Abwesenheits-Deckung pro Tag (0..1) und tagesgenaue Soll-Minuten
            # je Kategorie (nur Soll-Modus relevant).
            absent_frac: dict[date, float] = {}
            absent_frac_vacation: dict[date, float] = {}
            # Sonderurlaub OHNE zugeordnete Lohnart gilt als bezahlt-anwesend
            # (#67, Entscheidung B-Fallback): kein Soll-Abzug, keine Zeile —
            # der Anteil wird im Soll-Block wie gearbeitete Zeit gewertet,
            # damit der Tag nicht als Minus zaehlt.
            special_paid_frac: dict[date, float] = {}
            abs_min_vacation = 0
            abs_min_special = 0
            abs_min_sick: dict[str | None, int] = {}
            special_resolvable = (
                resolve_employee_lohnart(
                    wage_types, profiles, profile_by_employee.get(uid), "SONDERURLAUB_STD"
                )
                is not None
            )
            for d_, frac_, kind_, status_ in absence_day_entries.get(uid, []):
                if kind_ == "special" and not special_resolvable:
                    special_paid_frac[d_] = min(1.0, special_paid_frac.get(d_, 0.0) + frac_)
                    continue
                absent_frac[d_] = min(1.0, absent_frac.get(d_, 0.0) + frac_)
                if kind_ == "vacation":
                    absent_frac_vacation[d_] = min(1.0, absent_frac_vacation.get(d_, 0.0) + frac_)
                if d_ < month_start:
                    continue  # Vorlauf-Tage (#71): nur Deckung, keine Zeilen
                m = int(round(_tagessoll(d_) * frac_))
                if kind_ == "vacation":
                    abs_min_vacation += m
                elif kind_ == "sick":
                    abs_min_sick[status_] = abs_min_sick.get(status_, 0) + m
                elif kind_ == "special":
                    abs_min_special += m
                # kind_ == "unpaid": nur Soll-Reduktion, keine Verguetungszeile.

            # #73: Trotz Urlaub gestempelt — ein VOLL durch Urlaub gedeckter
            # Tag mit Ist zaehlt normal (Ledger-Regel #22): die Urlaubs-
            # deckung entfaellt, die Urlaubsminuten des Tages werden
            # zurueckgegeben. Teil-Urlaub (z.B. halbtags) deckt weiter.
            for d_, vfrac in absent_frac_vacation.items():
                if vfrac >= 0.999 and ist_by_day.get(d_, 0) > 0:
                    absent_frac[d_] = max(0.0, absent_frac.get(d_, 0.0) - vfrac)
                    if d_ >= month_start:
                        abs_min_vacation -= int(round(_tagessoll(d_) * vfrac))

            if export_basis == "soll":
                # ── LODAS-kompatibel (wie Altsystem/Altsystem) ──────────────────
                # 011 = effektive Sollzeit: Tagessoll aller Arbeitstage (Mo–Fr,
                # kein Feiertag) minus Abwesenheits-Anteil. Mehrarbeit = Ist −
                # Soll je Tag, woechentliches Freikontingent ohne Zuschlag,
                # Rest mit Zuschlag; Nacht-Anteil laeuft separat (084).
                # Negative Tage sammeln als Minusstunden (013).
                # Mehrarbeit-Auszahlung ist Modell-abhaengig (Spec 2026-07-09):
                # above_cap/none -> Differenzen leben ausschliesslich im
                # Ueberstunden-Ledger (Auszahlung ggf. als System-Buchung aus
                # dem Monatsabschluss, siehe payroll_bookings-Bypass unten).
                # Nur bei "all" bucht der Export Staffel/Minus wie bisher.
                payout_mode = (user.overtime_payout_mode or "above_cap").lower()
                # Profil-Override des woechentlichen Freikontingents (Regel
                # "mehrarbeit_frei_wochen_std"); ohne Override der Mandanten-Wert.
                # WE/Feiertags-Ist zaehlt nur mit Profil-Regel in die Staffel
                # (sonst laeuft es allein ueber die Zuschlags-Lohnarten, #21).
                emp_weekend_counts = resolve_employee_weekend_counts(
                    profiles, profile_by_employee.get(uid)
                )
                emp_frei_min = resolve_employee_mehrarbeit_frei_min(
                    profiles, profile_by_employee.get(uid), mehrarbeit_frei_min
                )
                soll_sum = 0
                minus_sum = 0
                week_quota: dict[tuple[int, int], int] = {}
                # #71: verbrauchtes Freikontingent der angebrochenen ISO-Woche
                # aus den Vorlauf-Tagen vorbelegen (gleiche Tageslogik wie
                # unten, aber ohne Zeilen/Soll/Minus).
                for day in lead_days:
                    holiday = is_holiday(day, bundesland)
                    ist = ist_by_day.get(day, 0)
                    if day.weekday() < 5 and not holiday:
                        ist_eff = ist + int(
                            round(_tagessoll(day) * special_paid_frac.get(day, 0.0))
                        )
                        diff = ist_eff - int(
                            round(_tagessoll(day) * (1.0 - absent_frac.get(day, 0.0)))
                        )
                    else:
                        diff = ist if emp_weekend_counts else 0
                    if diff <= 0:
                        continue
                    staffel = max(diff - night_by_day.get(day, 0), 0)
                    wk = day.isocalendar()[:2]
                    rest_frei = max(emp_frei_min - week_quota.get(wk, 0), 0)
                    week_quota[wk] = week_quota.get(wk, 0) + min(staffel, rest_frei)
                mehr_0 = 0
                mehr_z = 0
                for day in month_days:
                    holiday = is_holiday(day, bundesland)
                    ist = ist_by_day.get(day, 0)
                    if day.weekday() < 5 and not holiday:
                        frac_abs = absent_frac.get(day, 0.0)
                        day_soll = int(round(_tagessoll(day) * (1.0 - frac_abs)))
                        soll_sum += day_soll
                        # Unzugeordneter Sonderurlaub zaehlt wie gearbeitet (#67).
                        ist_eff = ist + int(
                            round(_tagessoll(day) * special_paid_frac.get(day, 0.0))
                        )
                        diff = ist_eff - day_soll
                        if frac_abs >= 1.0 and ist == 0:
                            continue  # ganztags abwesend, nichts zu verrechnen
                    else:
                        # Wochenende/Feiertag: kein Soll — Ist zaehlt nur mit
                        # Profil-Regel wochenende_ins_konto als Mehrarbeit.
                        diff = ist if emp_weekend_counts else 0
                    if payout_mode != "all":
                        continue  # Differenzen leben im Ueberstunden-Ledger
                    if diff > 0:
                        staffel = max(diff - night_by_day.get(day, 0), 0)
                        wk = day.isocalendar()[:2]
                        rest_frei = max(emp_frei_min - week_quota.get(wk, 0), 0)
                        frei_teil = min(staffel, rest_frei)
                        week_quota[wk] = week_quota.get(wk, 0) + frei_teil
                        mehr_0 += frei_teil
                        mehr_z += staffel - frei_teil
                    elif diff < 0:
                        minus_sum += -diff

                _add(pnr, "NORMALE_ARBEITSZEIT", soll_sum, uid)
                if payout_mode == "all":
                    _add(pnr, "MEHRARBEIT_0", mehr_0, uid)
                    _add(pnr, "MEHRARBEIT_ZUSCHLAG", mehr_z, uid)
                    _add(pnr, "MINUSSTUNDEN", minus_sum, uid)
                # Zuschlags-Overlays (Sa/So/Feiertag/Nacht) wie gehabt aus dem Ist.
                for code in ("SAMSTAG_ARBEIT", "SONNTAG_ARBEIT", "FEIERTAG_ARBEIT", "NACHT_ARBEIT"):
                    _add(pnr, code, totals.get(code, 0), uid)
                # Feiertag bezahlt: Tagessoll je unbearbeitetem Feiertag Mo–Fr.
                ft_min = sum(
                    _tagessoll(h)
                    for h in month_holidays
                    if h.weekday() < 5
                    and h not in worked_dates
                    and h not in absent_dates.get(uid, set())
                )
                _add(pnr, "FEIERTAG_BEZAHLT", ft_min, uid)
                # Urlaub/Krank: tagesgenaues Soll (Freitag zaehlt 5:30 etc.).
                _add(pnr, "URLAUB_STD", abs_min_vacation, uid)
                _add(pnr, "SONDERURLAUB_STD", abs_min_special, uid)
                sick_fallback_min = 0
                for status_key, sick_min in abs_min_sick.items():
                    code = SICK_STATUS_CATEGORY.get(status_key or "")
                    if code and (
                        resolve_employee_lohnart(
                            wage_types, profiles, profile_by_employee.get(uid), code
                        )
                        is not None
                    ):
                        _add(pnr, code, sick_min, uid)
                    else:
                        sick_fallback_min += sick_min
                _add(pnr, "KRANK_STD", sick_fallback_min, uid)
                continue

            # ── Ist-Basis (bisheriges Verhalten) ─────────────────────────────
            for code, minutes in totals.items():
                _add(pnr, code, minutes, uid)
            _add(
                pnr,
                "FEIERTAG_BEZAHLT",
                paid_holiday_minutes(
                    month_holidays, worked_dates, absent_dates.get(uid, set()), daily_target
                ),
                uid,
            )
            emp_abs = abs_days.get(uid, {})
            _add(
                pnr, "URLAUB_STD", absence_minutes(emp_abs.get("vacation", 0.0), daily_target), uid
            )
            # Krank: nach AU-Status aufgeschlüsselt. Hat der Status eine eigene
            # zugeordnete Lohnart (Profil oder Standard), wird sie gebucht —
            # sonst sammeln die Tage auf KRANK_STD (bisheriges Verhalten).
            sick_fallback_days = 0.0
            for status_key, sick_days in sick_by_status.get(uid, {}).items():
                code = SICK_STATUS_CATEGORY.get(status_key or "")
                if code and (
                    resolve_employee_lohnart(
                        wage_types, profiles, profile_by_employee.get(uid), code
                    )
                    is not None
                ):
                    _add(pnr, code, absence_minutes(sick_days, daily_target), uid)
                else:
                    sick_fallback_days += sick_days
            _add(pnr, "KRANK_STD", absence_minutes(sick_fallback_days, daily_target), uid)

    # Manuelle Lohnbuchungen (Pauschalen/Beträge/Sonderzahlungen) einmischen.
    # Reine Export-Positionen — Wert kann negativ sein (Abzug).
    from zeiterfassung.models.payroll_booking import PayrollBooking

    b_stmt = select(PayrollBooking).where(
        PayrollBooking.tenant_id == tenant_id,
        PayrollBooking.year_month == year_month,
    )
    if employee_ids:
        b_stmt = b_stmt.where(PayrollBooking.employee_id.in_(employee_ids))
    bookings = (await db.execute(b_stmt)).scalars().all()
    # Lohnbuchungen sind eine optionale Funktion (Feature-Schalter, Default AUS):
    # ohne aktiviertes Feature fliessen manuelle Buchungen NICHT in den Export.
    # System-Buchungen (Ueberstunden-Auszahlung aus dem Monatsabschluss) laufen
    # aber immer in den Export, unabhaengig vom Feature-Schalter (Spec 2026-07-09).
    _features = ((_tenant_row.settings or {}).get("features") or {}) if _tenant_row else {}
    if _features.get("payroll_bookings", False) is not True:
        bookings = [b for b in bookings if b.wage_type_code == "UEBERSTUNDEN_AUSZAHLUNG"]
    if allowed_by_type is not None:
        bookings = [b for b in bookings if b.employee_id in allowed_by_type]
    if bookings:
        emp_ids = {b.employee_id for b in bookings}
        _booking_users = list(
            (await db.execute(select(User).where(User.id.in_(emp_ids)))).scalars().all()
        )
        # Ohne Personalnummer keine Buchungszeile (siehe Zeilen-Erzeugung oben).
        _booking_users = [u for u in _booking_users if (u.employee_number or "").strip()]
        # Erfassung ohne Soll (Werkstudent/Aushilfe): grundsaetzlich nie im
        # DATEV-Export — auch nicht ueber den Buchungs-Seitenweg (Abrechnung
        # laeuft von Hand ueber den Monats-Stundenzettel, Absprache 12.08.2026).
        _booking_users = [u for u in _booking_users if hat_tagessoll(u.employment_type)]
        pnr_map = {u.id: u.employee_number for u in _booking_users}
        _allowed_booking_ids = {u.id for u in _booking_users}
        bookings = [b for b in bookings if b.employee_id in _allowed_booking_ids]
        for _u in _booking_users:
            profile_by_employee.setdefault(_u.id, _u.wage_profile_id)
        for b in bookings:
            la = resolve_employee_lohnart(
                wage_types, profiles, profile_by_employee.get(b.employee_id), b.wage_type_code
            )
            if la is None:
                continue
            val = round(float(b.value), 2)
            if val == 0:
                continue
            rows.append(
                DatevRow(
                    personalnummer=pnr_map.get(b.employee_id, str(b.employee_id)[:8]),
                    lohnart=la,
                    wert=val,
                    abrechnungszeitraum=year_month,
                )
            )

    if target_ids and skipped_no_pnr:
        _elog.warning(
            "DATEV-Export: %d Mitarbeiter ohne Personalnummer uebersprungen: %s",
            len(skipped_no_pnr),
            ", ".join(skipped_no_pnr),
        )

    # Zeilen-Ordnung wie das Altsystem: pro Mitarbeiter aufsteigend nach Wert,
    # bei Gleichstand nach Lohnart (066 vor 067). Mitarbeiter untereinander
    # nach Personalnummer (numerisch, sonst alphabetisch) — Des Altsystems interne
    # Reihenfolge ist nicht ableitbar, diese ist deterministisch.
    def _pnr_key(pnr: str) -> tuple[int, int | str]:
        return (0, int(pnr)) if pnr.isdigit() else (1, pnr)

    rows.sort(key=lambda r: (_pnr_key(r.personalnummer), r.wert, r.lohnart))
    return rows


def build_lodas_filename(year: int, month: int, company_name: str | None) -> str:
    """Dateiname wie im Altsystem: ``2026-05_BETA_LODAS.txt`` (Firma in
    Grossbuchstaben, Umlaute transkribiert, Sonderzeichen -> ``_``). Ohne
    Firma der neutrale Fallback."""
    if not company_name or not company_name.strip():
        return f"LODAS_{year}-{month:02d}.txt"
    slug = company_name.strip().upper()
    for a, b in (("\u00c4", "AE"), ("\u00d6", "OE"), ("\u00dc", "UE"), ("\u00df", "SS")):
        slug = slug.replace(a, b)
    slug = "".join(ch if ch.isalnum() else "_" for ch in slug).strip("_") or "EXPORT"
    return f"{year}-{month:02d}_{slug}_LODAS.txt"


async def _lodas_config_for_company(
    db: AsyncSession, tenant_id: uuid.UUID, company_id: uuid.UUID | None
) -> LodasConfig:
    """Berater-/Mandanten-Nr. aus der gewählten Firma (DATEV-Mandant = Firma).

    Ohne Firma bzw. ohne gepflegte Nummern ist kein gültiger Export möglich.
    """
    if company_id is None:
        raise ValueError(
            "Bitte eine Firma wählen — der Export braucht die Berater-/Mandanten-Nr. der Firma."
        )
    from zeiterfassung.models.company import Company

    co = (
        await db.execute(
            select(Company).where(Company.id == company_id, Company.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    if co is None:
        raise ValueError("Firma nicht gefunden.")
    if not co.advisor_number or not co.client_number:
        raise ValueError(
            f"Für die Firma '{co.name}' fehlen Berater-Nr. und/oder Mandanten-Nr. "
            "(in Einstellungen → Firmen pflegen)."
        )
    return LodasConfig(berater_nr=str(co.advisor_number), mandanten_nr=str(co.client_number))


async def _company_employee_ids(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    company_id: uuid.UUID,
    requested: list[uuid.UUID] | None,
) -> list[uuid.UUID]:
    """Mitarbeiter der Firma (ggf. geschnitten mit der übergebenen Auswahl)."""
    ids = set(
        (
            await db.execute(
                select(User.id).where(User.tenant_id == tenant_id, User.company_id == company_id)
            )
        )
        .scalars()
        .all()
    )
    if requested:
        ids &= set(requested)
    return list(ids)


async def preview_export(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    req: DatevExportRequest,
) -> DatevPreviewResponse:
    # Vorschau = gleiche Mitarbeiter-Menge wie Generate: bei gewählter Firma auf
    # deren Mitarbeiter einschränken (leer -> leere Vorschau statt "alle").
    emp_ids = req.employee_ids
    if req.company_id is not None:
        emp_ids = await _company_employee_ids(db, tenant_id, req.company_id, req.employee_ids)
        if not emp_ids:
            return DatevPreviewResponse(
                year_month=date(req.year, req.month, 1), row_count=0, rows=[]
            )
    wage_types = await _get_wage_type_mappings(db, tenant_id)
    rows = await _build_datev_rows(
        db,
        tenant_id,
        req.year,
        req.month,
        emp_ids,
        wage_types,
        employment_type=req.employment_type,
    )
    preview_rows = preview_datev_export(rows)
    return DatevPreviewResponse(
        year_month=date(req.year, req.month, 1),
        row_count=len(rows),
        rows=[
            {  # type: ignore[arg-type]
                "personalnummer": r["personalnummer"],
                "lohnart": r["lohnart"],
                "wert": r["wert"],
                "datum_von": r["datum_von"],
                "datum_bis": r["datum_bis"],
                "kostenstelle": r.get("kostenstelle", ""),
            }
            for r in preview_rows
        ],
    )


async def _assert_no_forgotten_clockouts(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    employee_ids: list[uuid.UUID],
    year: int,
    month: int,
) -> None:
    """Verweigert den Export, solange im Monat noch unaufgelöste Auto-Timeout-
    Ausstempel („Ausstempeln vergessen") liegen (Spec 2026-08-03, Entscheidung 1)."""
    from zeiterfassung.models.tenant import Tenant
    from zeiterfassung.services.clock_service import unresolved_auto_clockout_events
    from zeiterfassung.services.tenant_service import tenant_timezone

    tenant = await db.get(Tenant, tenant_id)
    tz = tenant_timezone(tenant)
    first = date(year, month, 1)
    nxt = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    start_utc = datetime(first.year, first.month, first.day, tzinfo=tz).astimezone(UTC)
    end_utc = datetime(nxt.year, nxt.month, nxt.day, tzinfo=tz).astimezone(UTC)
    open_evts = await unresolved_auto_clockout_events(
        db, tenant_id, employee_ids, start_utc, end_utc
    )
    if open_evts:
        raise ValueError(
            f"Export nicht möglich: {len(open_evts)} Tag(e) mit vergessenem "
            "Ausstempeln sind noch nicht aufgelöst. Bitte diese Tage zuerst "
            "korrigieren (echte Endzeit eintragen)."
        )


async def generate_export(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    req: DatevExportRequest,
    generated_by: uuid.UUID,
) -> tuple[PayrollExport, bytes]:
    """Generate and persist a LODAS export; return the export record and file bytes."""
    # DATEV-Mandant = Firma: Berater-/Mandanten-Nr. kommen aus der gewählten Firma.
    config = await _lodas_config_for_company(db, tenant_id, req.company_id)
    # Export nur für Mitarbeiter dieser Firma (ein Mandant = eine Firma).
    emp_ids = await _company_employee_ids(db, tenant_id, req.company_id, req.employee_ids)
    if not emp_ids:
        # Leere Liste darf NICHT als "alle" durchrutschen (falsche Mandanten-Nr.!).
        raise ValueError("Für die gewählte Firma sind keine (passenden) Mitarbeiter vorhanden.")

    # Export-Sperre (Spec 2026-08-03): Tage mit vergessenem Ausstempeln, die HR
    # noch nicht aufgelöst hat, dürfen nicht in die Abrechnung fließen — die
    # echten Stunden sind unbekannt (0 h wäre falsch). Erst korrigieren, dann
    # exportieren.
    await _assert_no_forgotten_clockouts(db, tenant_id, emp_ids, req.year, req.month)

    wage_types = await _get_wage_type_mappings(db, tenant_id)
    rows = await _build_datev_rows(
        db,
        tenant_id,
        req.year,
        req.month,
        emp_ids,
        wage_types,
        employment_type=req.employment_type,
    )
    lodas_bytes = write_lodas_export(rows, config)
    checksum = compute_checksum(lodas_bytes)
    now = datetime.now(tz=UTC)

    from zeiterfassung.models.company import Company

    company_name = (
        await db.execute(
            select(Company.name).where(Company.id == req.company_id, Company.tenant_id == tenant_id)
        )
    ).scalar()

    export = PayrollExport(
        tenant_id=tenant_id,
        year_month=date(req.year, req.month, 1),
        status=ExportStatus.GENERATED,
        file_name=build_lodas_filename(req.year, req.month, company_name),
        file_checksum=checksum,
        file_content=lodas_bytes,  # Original speichern -> Download liefert exakt diese Datei
        row_count=len(rows),
        generated_by=generated_by,
        generated_at=now,
        created_at=now,
        updated_at=now,
    )
    db.add(export)
    await db.flush()

    await audit_service.log(
        db,
        tenant_id=tenant_id,
        actor_id=generated_by,
        action="datev.export",
        resource_type="payroll_export",
        resource_id=export.id,
        details={"year": req.year, "month": req.month, "row_count": len(rows)},
    )
    return export, lodas_bytes


async def list_exports(
    db: AsyncSession, tenant_id: uuid.UUID, skip: int = 0, limit: int = 50
) -> list[PayrollExport]:
    result = await db.execute(
        select(PayrollExport)
        .where(PayrollExport.tenant_id == tenant_id)
        .order_by(PayrollExport.created_at.desc())
        .offset(skip)
        .limit(limit)
    )
    return list(result.scalars().all())


async def get_export(db: AsyncSession, tenant_id: uuid.UUID, export_id: uuid.UUID) -> PayrollExport:
    result = await db.execute(
        select(PayrollExport).where(
            PayrollExport.tenant_id == tenant_id,
            PayrollExport.id == export_id,
        )
    )
    export = result.scalar_one_or_none()
    if export is None:
        raise ExportError(f"Export {export_id} not found")
    return export
