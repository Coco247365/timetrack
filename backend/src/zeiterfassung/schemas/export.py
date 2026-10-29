"""Export schemas."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field

from zeiterfassung.models.export import ExportFormat, ExportStatus


class DatevExportRequest(BaseModel):
    year: int = Field(ge=2000, le=2100)
    month: int = Field(ge=1, le=12)
    company_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "DATEV-Mandant = Firma; für Generate erforderlich (liefert Berater-/Mandanten-Nr.)"
        ),
    )
    employee_ids: list[uuid.UUID] | None = Field(
        default=None, description="Restrict export to specific employees; None = all"
    )
    employment_type: str | None = Field(
        default="hourly",
        pattern="^(hourly|salaried)$",
        description=(
            "Mitarbeiterkreis: 'hourly' = nur Lohn-Empfänger (Standard), "
            "'salaried' = nur Gehaltsempfänger, null = alle."
        ),
    )


class DatevPreviewRow(BaseModel):
    personalnummer: str
    lohnart: int
    wert: float
    datum_von: date
    datum_bis: date
    kostenstelle: str = ""


class DatevPreviewResponse(BaseModel):
    year_month: date
    row_count: int
    rows: list[DatevPreviewRow]


class PayrollExportResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    tenant_id: uuid.UUID
    year_month: date
    format: ExportFormat
    status: ExportStatus
    file_checksum: str | None
    file_name: str | None = None
    row_count: int | None
    generated_by: uuid.UUID | None
    generated_at: datetime | None
    delivered_at: datetime | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime
