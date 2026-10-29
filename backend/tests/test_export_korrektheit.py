"""Export-Korrektheits-Paket (#67/#69/#71/#73): Soll-Modus-Randfaelle.

Alle Tests: 40h-Woche (Mo-Fr 8h), Bundesland HH, export_basis 'soll',
payout_mode 'all' (Alpha-Modell: Staffel/Minus werden direkt gebucht).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from zeiterfassung.models.absence import RequestType, SickStatus
from zeiterfassung.models.clock import EventSource, EventType, PunchEvent
from zeiterfassung.policy.context import DayPart
from zeiterfassung.schemas.absence import AbsenceRequestCreate
from zeiterfassung.services.absence_service import create_request
from zeiterfassung.services.export_service import _build_datev_rows

WAGE_TYPES = {
    "NORMALE_ARBEITSZEIT": 11,
    "MEHRARBEIT_0": 62,
    "MEHRARBEIT_ZUSCHLAG": 66,
    "MINUSSTUNDEN": 13,
    "URLAUB_STD": 129,
    "KRANK_STD": 153,
    "FEIERTAG_BEZAHLT": 130,
}
WAGE_TYPES_MIT_SONDERURLAUB = {**WAGE_TYPES, "SONDERURLAUB_STD": 140}


def _utc(d: date, hhmm: str) -> datetime:
    """Lokale Berliner Zeit (Sommerzeit, UTC+2) -> UTC."""
    h, m = map(int, hhmm.split(":"))
    local_minutes = h * 60 + m - 120
    return datetime(d.year, d.month, d.day, local_minutes // 60, local_minutes % 60, tzinfo=UTC)


def _soll_mode(tenant) -> None:
    tenant.settings = {
        **(tenant.settings or {}),
        "zuschlaege": {"export_basis": "soll", "mehrarbeit_frei_wochen_std": 1.5},
    }


def _punch_day(db, tenant, employee, d: date, kommt: str, geht: str, pause_min: int = 45):
    """Ein Arbeitstag mit Mittagspause 12:00 + pause_min."""
    now = datetime.now(tz=UTC)
    pe = 12 * 60 + pause_min
    for etype, t in (
        (EventType.PUNCH_IN, kommt),
        (EventType.BREAK_START, "12:00"),
        (EventType.BREAK_END, f"{pe // 60}:{pe % 60:02d}"),
        (EventType.PUNCH_OUT, geht),
    ):
        db.add(
            PunchEvent(
                tenant_id=tenant.id,
                employee_id=employee.id,
                event_type=etype,
                event_time=_utc(d, t),
                source=EventSource.NFC_TERMINAL,
                created_at=now,
            )
        )


async def _absence(db, tenant, employee, rtype, frm: date, to: date, day_part=DayPart.FULL):
    data = AbsenceRequestCreate.model_construct(
        request_type=rtype,
        date_from=frm,
        date_to=to,
        day_part=day_part,
        hours=None,
        sick_status=None,
        payload=None,
        # Sonderurlaub verlangt seit Spec 2026-08-11 eine Begründung.
        notes="Test" if rtype == RequestType.SPECIAL_LEAVE else None,
    )
    await create_request(
        db, tenant.id, employee.id, data, "HH", auto_approve=True, approver_id=uuid.uuid4()
    )


async def _rows(db, tenant, employee, year, month, wage_types=WAGE_TYPES) -> dict[int, float]:
    await db.commit()
    rows = await _build_datev_rows(db, tenant.id, year, month, [employee.id], wage_types)
    out: dict[int, float] = {}
    for r in rows:
        out[r.lohnart] = round(out.get(r.lohnart, 0.0) + r.wert, 2)
    return out


class TestUnbezahlterUrlaub:  # Issue #67
    async def test_unpaid_reduziert_soll_ohne_verguetungszeile(self, db, tenant, employee):
        """Juni 2026: 22 Arbeitstage (176h). 1 Tag unbezahlt (Mo 15.06.) ->
        011 = 168h, kein Minus fuer den Tag, keine sonstige Zeile."""
        _soll_mode(tenant)
        employee.overtime_payout_mode = "all"
        await _absence(
            db,
            tenant,
            employee,
            RequestType.UNPAID_LEAVE,
            date(2026, 6, 15),
            date(2026, 6, 15),
        )
        by = await _rows(db, tenant, employee, 2026, 6)
        # 21 unbearbeitete Arbeitstage -> Minus; der unbezahlte Tag NICHT.
        assert by == {11: 168.0, 13: 168.0}

    async def test_sonderurlaub_ohne_zuordnung_bleibt_bezahlt_in_011(self, db, tenant, employee):
        """Entscheidung B-Fallback: keine SONDERURLAUB-Lohnart zugeordnet ->
        Tag bleibt voll in 011, KEIN Minus fuer den Tag, keine Zeile."""
        _soll_mode(tenant)
        employee.overtime_payout_mode = "all"
        await _absence(
            db,
            tenant,
            employee,
            RequestType.SPECIAL_LEAVE,
            date(2026, 6, 15),
            date(2026, 6, 15),
        )
        by = await _rows(db, tenant, employee, 2026, 6)
        assert by == {11: 176.0, 13: 168.0}

    async def test_sonderurlaub_mit_zuordnung_bekommt_eigene_zeile(self, db, tenant, employee):
        _soll_mode(tenant)
        employee.overtime_payout_mode = "all"
        await _absence(
            db,
            tenant,
            employee,
            RequestType.SPECIAL_LEAVE,
            date(2026, 6, 15),
            date(2026, 6, 15),
        )
        by = await _rows(db, tenant, employee, 2026, 6, WAGE_TYPES_MIT_SONDERURLAUB)
        assert by == {11: 168.0, 13: 168.0, 140: 8.0}


class TestFeiertagImUrlaub:  # Issue #69
    async def test_feiertag_im_urlaub_wird_als_feiertag_bezahlt(self, db, tenant, employee):
        """Mai 2026 (HH): Feiertage 01.05. (Fr), 14.05. (Do), 25.05. (Mo) ->
        18 Arbeitstage (144h). Urlaub 30.04.-03.05. deckt den 01.05.:
        Feiertag zaehlt trotzdem in 130, kein Urlaubstag faellt in den Mai."""
        _soll_mode(tenant)
        employee.overtime_payout_mode = "all"
        await _absence(
            db,
            tenant,
            employee,
            RequestType.VACATION,
            date(2026, 4, 30),
            date(2026, 5, 3),
        )
        by = await _rows(db, tenant, employee, 2026, 5)
        assert by == {11: 144.0, 13: 144.0, 130: 24.0}

    async def test_feiertag_im_unbezahlten_urlaub_bleibt_unbezahlt(self, db, tenant, employee):
        _soll_mode(tenant)
        employee.overtime_payout_mode = "all"
        await _absence(
            db,
            tenant,
            employee,
            RequestType.UNPAID_LEAVE,
            date(2026, 4, 30),
            date(2026, 5, 3),
        )
        by = await _rows(db, tenant, employee, 2026, 5)
        # 01.05. blockt: nur 14.05. + 25.05. bezahlt.
        assert by == {11: 144.0, 13: 144.0, 130: 16.0}


class TestTrotzUrlaubGestempelt:  # Issue #73 (Ledger-Regel #22)
    async def test_voller_urlaubstag_mit_arbeit_zaehlt_normal(self, db, tenant, employee):
        """Voll genehmigter Urlaubstag (Mo 15.06.) mit exakt 8h Arbeit:
        Urlaub deckt NICHT (Tag zaehlt normal, Urlaubstag zurueckgegeben) ->
        volles Monatssoll in 011, KEINE Urlaubszeile, keine Mehrarbeit."""
        _soll_mode(tenant)
        employee.overtime_payout_mode = "all"
        await _absence(
            db, tenant, employee, RequestType.VACATION, date(2026, 6, 15), date(2026, 6, 15)
        )
        _punch_day(db, tenant, employee, date(2026, 6, 15), "08:00", "16:45")  # 8h netto
        by = await _rows(db, tenant, employee, 2026, 6)
        assert by == {11: 176.0, 13: 168.0}

    async def test_halber_urlaubstag_mit_halber_arbeit_bleibt_gedeckt(self, db, tenant, employee):
        """Halber Urlaub + halber Arbeitstag ist der Normalfall: deckt weiter."""
        _soll_mode(tenant)
        employee.overtime_payout_mode = "all"
        await _absence(
            db,
            tenant,
            employee,
            RequestType.VACATION,
            date(2026, 6, 16),
            date(2026, 6, 16),
            day_part=DayPart.AM,
        )
        _punch_day(db, tenant, employee, date(2026, 6, 16), "08:00", "12:00", pause_min=0)  # 4h
        by = await _rows(db, tenant, employee, 2026, 6)
        # 011 = 176h - 4h Deckung = 172h; Urlaub 4h; 21 leere Arbeitstage Minus.
        assert by == {11: 172.0, 129: 4.0, 13: 168.0}


class TestWochenkontingentMonatsgrenze:  # Issue #71
    async def test_iso_woche_ueber_monatswechsel_vergibt_kontingent_nur_einmal(
        self, db, tenant, employee
    ):
        """ISO-Woche 27/2026 = Mo 29.06.-So 05.07. Mehrarbeit +2h am Di 30.06.
        und +2h am Mi 01.07.: Juni bekommt 1,5h frei (062) + 0,5h Zuschlag
        (066); der Juli-Export sieht das verbrauchte Kontingent der Woche 27
        und bucht die vollen 2h als Zuschlag (066) — KEIN zweites 062."""
        _soll_mode(tenant)
        employee.overtime_payout_mode = "all"
        _punch_day(db, tenant, employee, date(2026, 6, 30), "08:00", "18:45")  # 10h
        _punch_day(db, tenant, employee, date(2026, 7, 1), "08:00", "18:45")  # 10h

        juni = await _rows(db, tenant, employee, 2026, 6)
        assert juni[62] == 1.5 and juni[66] == 0.5
        assert juni[11] == 176.0

        juli = await _rows(db, tenant, employee, 2026, 7)
        # Juli 2026: 23 Arbeitstage = 184h; 22 leere Tage Minus.
        assert juli == {11: 184.0, 66: 2.0, 13: 176.0}


class TestFeiertagImKrank:  # Altsystem-Beleg PN52 Mai 2026
    async def test_feiertag_in_krankschreibung_wird_als_krank_bezahlt(self, db, tenant, employee):
        """Krank 18.-29.05.2026 (AU_MIT_LFZ) ueber Pfingstmontag (25.05.):
        der Feiertag zaehlt als KRANK-Tagessoll (153), NICHT als 110.
        Mai 2026 (HH): 18 Arbeitstage (144h), Feiertage 01./14./25.05."""
        _soll_mode(tenant)
        employee.overtime_payout_mode = "all"
        data = AbsenceRequestCreate.model_construct(
            request_type=RequestType.SICK_LEAVE,
            date_from=date(2026, 5, 18),
            date_to=date(2026, 5, 29),
            day_part=DayPart.FULL,
            hours=None,
            sick_status=SickStatus.AU_MIT_LFZ,
            payload=None,
            notes=None,
        )
        await create_request(
            db,
            tenant.id,
            employee.id,
            data,
            "HH",
            auto_approve=True,
            approver_id=uuid.uuid4(),
        )
        by = await _rows(db, tenant, employee, 2026, 5)
        # 9 Krank-Arbeitstage (72h) + Feiertag 25.05. (8h) = 80h auf 153
        # (AU_MIT_LFZ faellt ohne eigene Zuordnung auf KRANK_STD zurueck);
        # 110 nur noch 01.05.+14.05. = 16h; 9 leere Arbeitstage Minus.
        assert by == {11: 72.0, 13: 72.0, 130: 16.0, 153: 80.0}


class TestZeilenSortierung:
    """Zeilen-Ordnung wie das Altsystem: pro Mitarbeiter aufsteigend nach Wert,
    Gleichstand nach Lohnart (Beleg: alle echten LODAS-Dateien 04-06/2026)."""

    async def test_zeilen_aufsteigend_nach_wert(self, db, tenant, employee):
        _soll_mode(tenant)
        employee.overtime_payout_mode = "all"
        data = AbsenceRequestCreate.model_construct(
            request_type=RequestType.SICK_LEAVE,
            date_from=date(2026, 5, 18),
            date_to=date(2026, 5, 29),
            day_part=DayPart.FULL,
            hours=None,
            sick_status=None,
            payload=None,
            notes=None,
        )
        await create_request(
            db, tenant.id, employee.id, data, "HH", auto_approve=True, approver_id=uuid.uuid4()
        )
        await db.commit()
        rows = await _build_datev_rows(db, tenant.id, 2026, 5, [employee.id], WAGE_TYPES)
        werte = [(r.wert, r.lohnart) for r in rows]
        assert werte == sorted(werte), werte
