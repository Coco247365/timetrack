"""Tenant model — top-level multi-tenancy unit."""

from __future__ import annotations

from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column

from zeiterfassung.models.base import (
    Base,
    JSONBCompat,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)


class Tenant(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A company or organisational unit using the Zeiterfassung platform."""

    __tablename__ = "tenants"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Europe/Berlin")
    locale: Mapped[str] = mapped_column(String(10), nullable=False, default="de-DE")
    bundesland: Mapped[str] = mapped_column(
        String(4), nullable=False, default="HH", comment="ISO 3166-2:DE Bundesland code"
    )
    settings: Mapped[dict | None] = mapped_column(JSONBCompat, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    def __repr__(self) -> str:
        return f"<Tenant id={self.id} name={self.name!r}>"
