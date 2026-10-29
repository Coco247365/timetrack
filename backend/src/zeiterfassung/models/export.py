"""Payroll export models: payroll_exports, wage_type_mappings."""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Enum, ForeignKey, Integer, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from zeiterfassung.models.base import Base, TimestampMixin, UUIDCompat, UUIDPrimaryKeyMixin


class ExportFormat(enum.StrEnum):
    DATEV_CSV = "datev_csv"


class ExportStatus(enum.StrEnum):
    PREVIEW = "PREVIEW"
    GENERATED = "GENERATED"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"


class PayrollExport(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Record of a payroll export run."""

    __tablename__ = "payroll_exports"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUIDCompat(),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    year_month: Mapped[date] = mapped_column(
        Date, nullable=False, comment="First day of the export month"
    )
    format: Mapped[ExportFormat] = mapped_column(
        Enum(
            ExportFormat,
            name="export_format_enum",
            native_enum=True,
            values_callable=lambda obj: [e.value for e in obj],
        ),
        nullable=False,
        default=ExportFormat.DATEV_CSV,
    )
    status: Mapped[ExportStatus] = mapped_column(
        Enum(ExportStatus, name="export_status_enum", native_enum=True),
        nullable=False,
        default=ExportStatus.PREVIEW,
    )
    file_checksum: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="SHA-256 hex digest"
    )
    file_content: Mapped[bytes | None] = mapped_column(
        LargeBinary, nullable=True, comment="Original erzeugte LODAS-Datei (fuer Download)"
    )
    file_name: Mapped[str | None] = mapped_column(
        String(120), nullable=True, comment="Download-Dateiname, z.B. 2026-05_BETA_LODAS.txt"
    )
    row_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    generated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUIDCompat(), ForeignKey("users.id"), nullable=True
    )
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<PayrollExport id={self.id} month={self.year_month} status={self.status}>"


class WageTypeMapping(Base):
    """Maps internal wage codes to DATEV Lohnart numbers."""

    __tablename__ = "wage_type_mappings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUIDCompat(),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    internal_code: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="e.g. REGULAR_HOURS, OVERTIME_HOURS"
    )
    datev_lohnart: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="DATEV Lohnart number (NULL = noch keine Nummer vergeben -> kein Export)",
    )
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    unit: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        default="hours",
        server_default="hours",
        comment="Einheit der Lohnart: hours | amount | days",
    )
    folge_lohnart: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="internal_code einer Begleit-Lohnart: jede Buchung erzeugt "
        "automatisch eine zweite Zeile mit gleichem Wert (z.B. 129->150, 66->67)",
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    def __repr__(self) -> str:
        return f"<WageTypeMapping code={self.internal_code!r} lohnart={self.datev_lohnart}>"
