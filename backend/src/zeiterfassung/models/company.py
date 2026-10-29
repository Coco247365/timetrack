"""Company model — a per-tenant lookup of companies (Firmen).

This is NOT a tenant/Mandant. It is a simple category assigned per employee so
the workforce can be grouped/filtered (e.g. in lists and the DATEV export).
"""

from __future__ import annotations

import uuid

from sqlalchemy import Boolean, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from zeiterfassung.models.base import Base, TimestampMixin, UUIDCompat, UUIDPrimaryKeyMixin


class Company(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A named company within a tenant (employee grouping)."""

    __tablename__ = "companies"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUIDCompat(),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # DATEV-Mandant pro Firma (getrennt je Firma fuer den Export).
    advisor_number: Mapped[str | None] = mapped_column(
        String(20), nullable=True, comment="DATEV Berater-Nr."
    )
    client_number: Mapped[str | None] = mapped_column(
        String(20), nullable=True, comment="DATEV Mandanten-Nr."
    )

    def __repr__(self) -> str:
        return f"<Company id={self.id} name={self.name!r}>"
