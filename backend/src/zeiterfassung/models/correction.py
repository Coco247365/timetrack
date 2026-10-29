"""Timesheet correction model."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column

from zeiterfassung.models.base import Base, BigIntCompat, UUIDCompat


class CorrectionType(enum.StrEnum):
    MANUAL_TIME = "MANUAL_TIME"
    BREAK_ADJUSTMENT = "BREAK_ADJUSTMENT"
    STATUS_CHANGE = "STATUS_CHANGE"
    NOTE_UPDATE = "NOTE_UPDATE"
    SUPPLEMENT_CORRECTION = "SUPPLEMENT_CORRECTION"


class TimesheetCorrection(Base):
    """Audit record of a correction applied to a daily timesheet."""

    __tablename__ = "timesheet_corrections"

    id: Mapped[int] = mapped_column(BigIntCompat(), primary_key=True, autoincrement=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUIDCompat(),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    timesheet_id: Mapped[int] = mapped_column(
        BigIntCompat(),
        ForeignKey("daily_timesheets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    correction_type: Mapped[CorrectionType] = mapped_column(
        Enum(CorrectionType, name="correction_type_enum", native_enum=True), nullable=False
    )
    field_changed: Mapped[str | None] = mapped_column(Text, nullable=True)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_by: Mapped[uuid.UUID] = mapped_column(
        UUIDCompat(), ForeignKey("users.id"), nullable=False
    )
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUIDCompat(), ForeignKey("users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    def __repr__(self) -> str:
        return (
            f"<TimesheetCorrection id={self.id} type={self.correction_type} "
            f"timesheet={self.timesheet_id}>"
        )
