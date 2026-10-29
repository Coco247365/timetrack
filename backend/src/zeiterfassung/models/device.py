"""NFC terminal device model for DB-backed authentication."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from zeiterfassung.models.base import (
    Base,
    TimestampMixin,
    UUIDCompat,
    UUIDPrimaryKeyMixin,
)


class Device(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An NFC terminal device registered to a tenant.

    Each device authenticates using a pre-shared key whose sha256 hex digest
    is stored in ``key_hash``.  The plaintext key is returned exactly once at
    registration and never persisted.
    """

    __tablename__ = "devices"

    tenant_id: Mapped[object] = mapped_column(
        UUIDCompat(),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    key_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
        index=True,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    location: Mapped[str | None] = mapped_column(String(255), nullable=True)

    def __repr__(self) -> str:
        return f"<Device id={self.id} name={self.name!r} tenant_id={self.tenant_id}>"
