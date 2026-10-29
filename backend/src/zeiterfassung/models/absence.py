"""Absence models: approval_requests, approval_audit_log."""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, Enum, ForeignKey, Numeric, Text
from sqlalchemy.orm import Mapped, mapped_column

from zeiterfassung.models.base import (
    Base,
    BigIntCompat,
    JSONBCompat,
    TimestampMixin,
    UUIDCompat,
    UUIDPrimaryKeyMixin,
)

# DayPart lebt in der (reinen) Policy-Schicht — hier wiederverwendet, damit der
# Tagesanteil ein gemeinsames Vokabular hat. policy.context importiert nichts aus
# models, also kein Zyklus.
from zeiterfassung.policy.context import DayPart


class RequestType(enum.StrEnum):
    VACATION = "VACATION"
    SICK_LEAVE = "SICK_LEAVE"
    UNPAID_LEAVE = "UNPAID_LEAVE"
    SPECIAL_LEAVE = "SPECIAL_LEAVE"
    CORRECTION = "CORRECTION"
    UNEXCUSED_ABSENCE = "UNEXCUSED_ABSENCE"  # unentschuldigtes Fehlen → zählt als Minus
    # Überstunden abbauen: Soll bleibt offen → Minus aufs Konto, aber geplant
    # und klar so benannt — taucht NIE als "unentschuldigt" auf (Spec 2026-08-11).
    OVERTIME_REDUCTION = "OVERTIME_REDUCTION"


class RequestStatus(enum.StrEnum):
    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    ESCALATED = "ESCALATED"
    CANCELLED = "CANCELLED"


class SickStatus(enum.StrEnum):
    """Art der Krankmeldung (AU/LFZ-Nachweis). Nur für SICK_LEAVE relevant.

    - EAU_TELEFON: telefonische Krankmeldung (elektronische AU folgt)
    - AU_MIT_LFZ: mit Arbeitsunfähigkeitsbescheinigung, mit Lohnfortzahlung
    - OHNE_AU_MIT_LFZ: ohne AU, mit Lohnfortzahlung
    - OHNE_AU_OHNE_LFZ: ohne AU, ohne Lohnfortzahlung
    """

    EAU_TELEFON = "EAU_TELEFON"
    AU_MIT_LFZ = "AU_MIT_LFZ"
    OHNE_AU_MIT_LFZ = "OHNE_AU_MIT_LFZ"
    OHNE_AU_OHNE_LFZ = "OHNE_AU_OHNE_LFZ"


class ActorType(enum.StrEnum):
    USER = "USER"
    SYSTEM = "SYSTEM"


class ApprovalRequest(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Leave or absence approval request submitted by an employee."""

    __tablename__ = "approval_requests"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUIDCompat(),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        UUIDCompat(),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    request_type: Mapped[RequestType] = mapped_column(
        Enum(RequestType, name="request_type_enum", native_enum=True), nullable=False
    )
    status: Mapped[RequestStatus] = mapped_column(
        Enum(RequestStatus, name="request_status_enum", native_enum=True),
        nullable=False,
        default=RequestStatus.DRAFT,
    )
    date_from: Mapped[date] = mapped_column(Date, nullable=False)
    date_to: Mapped[date] = mapped_column(Date, nullable=False)
    # Tagesanteil: ganzer Tag oder Halbtag (Vor-/Nachmittag). Urlaub wird
    # halbtags-genau geführt; die Stunden ergeben sich aus day_part × Soll/Tag.
    day_part: Mapped[DayPart] = mapped_column(
        Enum(DayPart, name="day_part_enum", native_enum=False, length=4),
        nullable=False,
        default=DayPart.FULL,
        server_default=DayPart.FULL.value,
    )
    days_count: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    # Art der Krankmeldung (AU/LFZ) — nur für SICK_LEAVE gesetzt, sonst NULL.
    sick_status: Mapped[SickStatus | None] = mapped_column(
        Enum(SickStatus, name="sick_status_enum", native_enum=False, length=20),
        nullable=True,
    )
    payload: Mapped[dict | None] = mapped_column(
        JSONBCompat,
        nullable=True,
        comment="Extra data: sick note upload ref, special leave type, etc.",
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        UUIDCompat(), ForeignKey("users.id"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<ApprovalRequest id={self.id} type={self.request_type} status={self.status}>"


class ApprovalAuditLog(Base):
    """Immutable audit trail for every status change on an approval request."""

    __tablename__ = "approval_audit_log"

    id: Mapped[int] = mapped_column(BigIntCompat(), primary_key=True, autoincrement=True)
    request_id: Mapped[uuid.UUID] = mapped_column(
        UUIDCompat(),
        ForeignKey("approval_requests.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    old_status: Mapped[RequestStatus | None] = mapped_column(
        Enum(RequestStatus, name="request_status_enum", native_enum=True), nullable=True
    )
    new_status: Mapped[RequestStatus] = mapped_column(
        Enum(RequestStatus, name="request_status_enum", native_enum=True), nullable=False
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDCompat(), ForeignKey("users.id"), nullable=True
    )
    actor_type: Mapped[ActorType] = mapped_column(
        Enum(ActorType, name="actor_type_enum", native_enum=True), nullable=False
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    def __repr__(self) -> str:
        return f"<ApprovalAuditLog request={self.request_id} {self.old_status}->{self.new_status}>"
