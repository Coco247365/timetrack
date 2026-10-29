"""Global audit log model."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from zeiterfassung.models.base import Base, BigIntCompat, JSONBCompat, UUIDCompat


class AuditLog(Base):
    """Append-only security and activity audit trail."""

    __tablename__ = "audit_log"

    # BigIntCompat: BIGINT auf Postgres, INTEGER auf SQLite (Autoincrement-Kompatibilitaet)
    id: Mapped[int] = mapped_column(BigIntCompat(), primary_key=True, autoincrement=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUIDCompat(),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDCompat(), ForeignKey("users.id"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    resource_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    details: Mapped[dict | None] = mapped_column(JSONBCompat, nullable=True)
    data_category: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
        comment="personal | medical | financial | aggregate | system",
    )
    access_reason: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
        comment="Required when data_category = medical",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    def __repr__(self) -> str:
        return f"<AuditLog id={self.id} action={self.action!r} actor={self.actor_id}>"
