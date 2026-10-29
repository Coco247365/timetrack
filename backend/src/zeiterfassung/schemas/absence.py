"""Absence / leave request schemas."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field, model_validator

from zeiterfassung.models.absence import RequestStatus, RequestType, SickStatus
from zeiterfassung.policy.context import DayPart


class AbsenceRequestCreate(BaseModel):
    request_type: RequestType
    date_from: date
    date_to: date
    # Tagesanteil: ganzer Tag (Default) oder Halbtag (AM/PM). Ein Halbtag über
    # mehrere Tage gilt für jeden Tag des Bereichs.
    day_part: DayPart = DayPart.FULL
    # Optionale explizite Stunden für einen Teil-Tag (Urlaub); überschreibt den
    # day_part-Anteil. Verbrauchter Tagesanteil = Stunden / Soll pro Tag.
    # Nur für einen EINZELNEN Tag erlaubt (siehe Validator).
    hours: float | None = Field(default=None, gt=0, le=24)
    # Art der Krankmeldung (AU/LFZ) — nur bei SICK_LEAVE sinnvoll.
    sick_status: SickStatus | None = None
    notes: str | None = Field(default=None, max_length=1024)
    payload: dict | None = None

    @model_validator(mode="after")
    def validate_date_range(self) -> AbsenceRequestCreate:
        if self.date_to < self.date_from:
            raise ValueError("date_to must be on or after date_from")
        # Teil-Tag in Stunden gilt nur für EINEN Tag — sonst würde der Stunden-
        # Anteil jeden Tag des Bereichs multiplizieren (4 h über 5 Tage = 2,5 Tage
        # statt eines Halbtags). Die UI erzwingt das bereits; hier die serverseitige
        # Spiegelung.
        if self.hours is not None and self.date_from != self.date_to:
            raise ValueError("hours erfordert einen einzelnen Tag (date_from == date_to)")
        # Security: prevent retroactive absence requests for past dates.
        # Exception: CORRECTION requests are inherently retroactive (an employee
        # corrects already-worked time), so past dates are allowed for them.
        from datetime import date as date_type

        today = date_type.today()
        if self.request_type != RequestType.CORRECTION and self.date_from < today:
            raise ValueError("Cannot submit absence requests for past dates")
        return self


class AbsenceRequestResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    tenant_id: uuid.UUID
    employee_id: uuid.UUID
    employee_name: str | None = None  # Populated by admin endpoints
    employee_number: str | None = None
    request_type: RequestType
    status: RequestStatus
    date_from: date
    date_to: date
    day_part: DayPart = DayPart.FULL
    days_count: Decimal
    sick_status: SickStatus | None = None
    notes: str | None
    submitted_at: datetime | None
    decided_by: uuid.UUID | None
    decided_at: datetime | None
    decision_reason: str | None
    created_at: datetime
    updated_at: datetime


class ApproveRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=1024)


class RejectRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=1024)


class VacationBalance(BaseModel):
    employee_id: uuid.UUID
    year: int
    entitlement_days: float
    used_days: float
    pending_days: float
    # Aufteilung: past_days = bereits genommen (Vergangenheit), planned_days = geplant
    # (genehmigt/beantragt, heute/künftig). offen(remaining) + past + planned = Anspruch.
    past_days: float = 0.0
    planned_days: float = 0.0
    remaining_days: float
    # Stunden-Sicht (halbtags-genau): Tage × Soll/Tag (= Wochenstunden/5).
    # Additiv zu den Tage-Feldern, damit bestehende Consumer nicht brechen.
    entitlement_hours: float = 0.0
    used_hours: float = 0.0
    pending_hours: float = 0.0
    remaining_hours: float = 0.0
