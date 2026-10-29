"""Approval delegation model."""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import CheckConstraint, Date, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from zeiterfassung.models.base import Base, TimestampMixin, UUIDCompat, UUIDPrimaryKeyMixin


class ApprovalDelegation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Temporary approval delegation from one user to another.

    Allows a Teamleiter or manager to delegate their approval authority
    for a specified date range (e.g. during vacation).
    """

    __tablename__ = "approval_delegations"
    __table_args__ = (
        CheckConstraint("valid_to >= valid_from", name="ck_delegation_date_order"),
        CheckConstraint("delegator_id != delegate_id", name="ck_delegation_no_self"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUIDCompat(),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    delegator_id: Mapped[uuid.UUID] = mapped_column(
        UUIDCompat(),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    delegate_id: Mapped[uuid.UUID] = mapped_column(
        UUIDCompat(),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[date] = mapped_column(Date, nullable=False)

    def __repr__(self) -> str:
        return (
            f"<ApprovalDelegation id={self.id} "
            f"delegator={self.delegator_id} delegate={self.delegate_id} "
            f"{self.valid_from}–{self.valid_to}>"
        )
