"""Response schemas for the admin reporting/approval endpoints.

Moved verbatim out of the former monolithic ``api/v1/admin.py``
(zeiterfassung-6pcv) so both ``services/report_service.py`` and
``api/v1/admin/reports.py`` can import them without a router<->service
circular import.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel

from zeiterfassung.schemas.absence import AbsenceRequestResponse, VacationBalance


class DashboardStats(BaseModel):
    total_active_users: int
    timesheets_flagged: int
    pending_approvals: int
    exports_this_month: int


class BulkApproveRequest(BaseModel):
    request_ids: list[uuid.UUID]
    reason: str | None = None


class AuditLogEntry(BaseModel):
    id: int
    actor_id: uuid.UUID | None
    action: str
    resource_type: str | None
    resource_id: str | None
    ip_address: str | None
    details: dict | None
    created_at: datetime


class OverlappingAbsence(BaseModel):
    employee_name: str
    employee_number: str | None
    request_type: str
    status: str
    date_from: date
    date_to: date
    days_count: float


class ApprovalContext(BaseModel):
    request: AbsenceRequestResponse
    vacation_balance: VacationBalance
    overlapping_absences: list[OverlappingAbsence]
    total_active_employees: int
    absent_during_period: int
    staffing_percentage: float
    staffing_warning: str | None


class EmployeeMonthSummary(BaseModel):
    employee_id: uuid.UUID
    employee_name: str
    employee_number: str | None
    days_worked: int
    total_work_minutes: int
    # Monatssaldo Ist − Soll (Plus und Minus verrechnet), Konto-Rechnung —
    # nicht mehr die Summe der Plus-Tage.
    total_overtime_minutes: int
    total_break_minutes: int
    days_vacation: float
    days_sick: float
    night_supplement_minutes: int = 0
    sunday_supplement_minutes: int = 0
    holiday_supplement_minutes: int = 0


class YearMonthSummary(BaseModel):
    month: int
    days_worked: int
    work_minutes: int
    overtime_minutes: int  # Monatssaldo Ist − Soll (s. EmployeeMonthSummary)
    days_vacation: float
    days_sick: float
    days_other_absence: float


class YearAbsenceEntry(BaseModel):
    id: uuid.UUID
    request_type: str
    date_from: date
    date_to: date
    day_part: str | None = None
    days_count: float
    sick_status: str | None = None


class EmployeeYearReport(BaseModel):
    employee_id: uuid.UUID
    employee_name: str
    employee_number: str | None
    year: int
    months: list[YearMonthSummary]
    absences: list[YearAbsenceEntry]


class TimesheetSegment(BaseModel):
    start: str  # gewertete lokale Zeit HH:MM
    end: str
    # Tatsaechlich gestempelt — nur gesetzt, wenn die Bewertung davon abweicht
    # (Profil-Regel "fruehester Beginn"): gestempelt 05:55, gewertet ab 06:00.
    start_punched: str | None = None
    clipped_earliest_start: bool = False
    # Analog fuer das ENDE (Stempel-Fenster eines NFC-Arbeitszeit-Profils,
    # Spec 2026-08-14): gestempelt 16:30, gewertet nur bis 16:00.
    end_punched: str | None = None
    clipped_window_end: bool = False


class TimesheetDay(BaseModel):
    date: date
    weekday: str  # Mo..So
    segments: list[TimesheetSegment]
    pause_minutes: int  # angesetzte Pause (nach ArbZG §4)
    # Tatsaechlich gestempelte Pause und ob die gesetzliche Mindestpause
    # angehoben hat — damit im Zettel beides sichtbar ist.
    pause_punched_minutes: int = 0
    pause_raised_by_law: bool = False
    ist_minutes: int
    soll_minutes: int
    diff_minutes: int
    absence: str | None  # "Urlaub" | "Krank" | None
    holiday: bool
    # Warnhinweis aus dem Tagesbogen (z. B. "Ausstempeln vergessen"): der Tag
    # zeigt dann seine Segmente, aber Ist 0 — wie im Tagesdetail.
    flagged_reason: str | None = None


class TimesheetSummary(BaseModel):
    soll_minutes: int
    ist_minutes: int
    saldo_minutes: int
    urlaub_minutes: int
    urlaub_tage: float
    krank_minutes: int
    krank_tage: float
    # Monatsabgleich (Altsystem-Fussblock): Bruttosoll aller Mo–Fr, davon
    # Feiertage und sonstige Abwesenheiten (Sonderurlaub/Unbezahlt), die das
    # Soll auf das effektive Soll (``soll_minutes``) druecken.
    brutto_soll_minutes: int = 0
    feiertag_minutes: int = 0
    feiertag_tage: float = 0.0
    sonstige_minutes: int = 0
    sonstige_tage: float = 0.0


class MonthlyTimesheet(BaseModel):
    employee_id: uuid.UUID
    employee_name: str
    employee_number: str | None
    year: int
    month: int
    days: list[TimesheetDay]
    summary: TimesheetSummary


# ── Monatsabgleich je Mitarbeiter (Auswertung, Spec 2026-09-09) ──────────────


class ReconciliationStunden(BaseModel):
    brutto_soll_minutes: int
    feiertag_minutes: int
    feiertag_tage: float
    urlaub_minutes: int
    urlaub_tage: float
    krank_minutes: int
    krank_tage: float
    sonstige_minutes: int
    sonstige_tage: float
    effektiv_soll_minutes: int
    ist_minutes: int
    saldo_minutes: int


class ReconciliationUeberstunden(BaseModel):
    payout_mode: str | None
    cap_hours: float | None
    vortrag_hours: float
    monat_hours: float
    ausgezahlt_hours: float
    korrektur_hours: float
    verfall_hours: float
    saldo_hours: float
    abgeschlossen: bool


class ReconciliationZuschlaege(BaseModel):
    nacht_minutes: int
    sonntag_minutes: int
    feiertag_minutes: int
    samstag_minutes: int


class ReconciliationLohnart(BaseModel):
    lohnart: int
    bezeichnung: str | None
    wert: float
    # Tage nur bei Abwesenheits-Lohnarten (Urlaub/Krank/Feiertag), wie auf
    # dem Altsystem-Blatt ("0110-Feiertagslohn 24:00 3.0 Tage").
    tage: float | None = None


class MonthReconciliation(BaseModel):
    employee_id: uuid.UUID
    employee_name: str
    employee_number: str | None
    year: int
    month: int
    stunden: ReconciliationStunden
    ueberstunden: ReconciliationUeberstunden
    zuschlaege: ReconciliationZuschlaege
    lohnarten: list[ReconciliationLohnart]
    export_status: str | None
    # Urlaubskonto (Resttage im Jahr des Monats), wie "01-Urlaubskonto" im Altsystem.
    urlaub_rest_tage: float | None = None
