"""Refresh-Token-Sessions — Rotation + Revocation (Bead zeiterfassung-fkw.2).

Jede ausgestellte Refresh-Token-JTI bekommt eine Zeile. Bei jedem erfolgreichen
Refresh wird die alte Zeile revoked (``revoked_at`` + ``rotated_to_jti``) und
eine neue Zeile mit derselben ``family_id`` angelegt (Rotation). Taucht eine
bereits revozierte/rotierte JTI erneut auf, ist das ein Replay eines gestohlenen
Tokens — die ganze Familie (alle Sessions mit derselben ``family_id``) wird
revoked (Reuse-Detection).

Diese Tabelle ist operativer Zustand (kein Lohn-/Zeit-Datum) — im Unterschied zu
``punch_events``/``overtime_ledger`` ist ein in-place ``UPDATE`` auf
``revoked_at``/``rotated_to_jti`` hier bewusst zulaessig (siehe
docs/plan-jwt-login-haertung.md, Abschnitt "Sessions als operativer Zustand").
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from zeiterfassung.models.base import (
    Base,
    UUIDCompat,
    UUIDPrimaryKeyMixin,
)


class RefreshSession(UUIDPrimaryKeyMixin, Base):
    """Eine ausgestellte Refresh-Token-Session (eine Zeile pro JTI)."""

    __tablename__ = "refresh_sessions"

    jti: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUIDCompat(),
        ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Spiegelt Account.tenant_id: nullable fuer Cross-Tenant-Superadmin-Accounts.
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDCompat(),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    family_id: Mapped[uuid.UUID] = mapped_column(
        UUIDCompat(),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rotated_to_jti: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)

    def __repr__(self) -> str:
        return f"<RefreshSession id={self.id} family_id={self.family_id} jti={self.jti[:8]}...>"
