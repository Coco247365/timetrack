"""Account and AccountRole models — login identities (separate from employees).

An *account* is a login that can access the web tool. It carries credentials and
roles/permissions. It may optionally link to an *employee* (the workforce record
in ``users``) for self-service; admins are accounts without an employee link.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from zeiterfassung.models.base import (
    Base,
    TimestampMixin,
    UUIDCompat,
    UUIDPrimaryKeyMixin,
)


class Account(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A login identity with access to the tool."""

    __tablename__ = "accounts"

    # Nullable tenant_id allows a cross-tenant super-admin ("Holding").
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDCompat(),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    is_superadmin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # Optional link to the person this login belongs to (self-service).
    # Zustaendige Bereiche (Liste von Department-UUIDs als Strings). Wenn gesetzt,
    # ueberschreibt das fuer alle Bereichs-Rechte (Genehmigen, Team-Sicht, ...)
    # die Abteilung der verknuepften Personalakte. None/leer = Personalakte zaehlt.
    scope_department_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    employee_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDCompat(),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    # Login-Lockout (fkw.4, DB-basiert, kein Redis): 10 Fehlversuche -> 5 min Sperre.
    # Siehe user_service.authenticate(). Beide Felder werden auch bei einem
    # fehlgeschlagenen Login-Versuch committet (nicht nur bei Erfolg).
    failed_login_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Sofortige Token-Entwertung (login-hardening, Spec 2026-07-20): der Wert
    # wandert als ver-Claim in jedes Access-Token; get_current_user vergleicht
    # pro Request. Ein Bump (invalidate_account_tokens) macht alle laufenden
    # Access-Tokens des Accounts sofort ungueltig.
    token_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    account_roles: Mapped[list[AccountRole]] = relationship(
        "AccountRole", back_populates="account", lazy="select", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Account id={self.id} email={self.email!r}>"


class AccountRole(Base):
    """Many-to-many join between accounts and roles."""

    __tablename__ = "account_roles"

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUIDCompat(),
        ForeignKey("accounts.id", ondelete="CASCADE"),
        primary_key=True,
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        UUIDCompat(),
        ForeignKey("roles.id", ondelete="CASCADE"),
        primary_key=True,
    )

    account: Mapped[Account] = relationship(
        "Account", back_populates="account_roles", lazy="select"
    )
    role: Mapped[object] = relationship("Role", lazy="select")
