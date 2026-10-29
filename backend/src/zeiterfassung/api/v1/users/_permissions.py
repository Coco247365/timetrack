"""Shared permission helpers for the ``users`` router package.

Moved verbatim out of the former monolithic ``api/v1/users.py`` (zeiterfassung-6pcv)
so ``core.py``, ``org.py`` and ``overtime.py`` can share the same checks without
duplicating (and risking drift of) the logic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from zeiterfassung.api.deps import AuthUser


def admin_can_read(u: AuthUser) -> bool:
    # Admin-Namespace (admin:dashboard = GF/HR/Buchhaltung, admin:settings = Verwaltung)
    # darf Verwaltungsdaten LESEN — vorher stand hier ein totes exaktes "admin".
    return (
        u.has_permission("users:read_all")
        or u.has_permission("users:write")
        or u.has_permission_namespace("admin")
    )


def admin_can_write(u: AuthUser) -> bool:
    return u.has_permission("admin:settings") or u.has_permission("users:write")


# Feinere Verwaltungs-Rechte: admin:settings deckt bewusst NICHT mehr alles ab.
# Superadmin darf immer alles (Bypass), damit man sich nicht aussperren kann.
def can_manage_roles(u: AuthUser) -> bool:
    """Rollen & Rechte bearbeiten — sonst koennte sich jeder mit users:write
    selbst mehr Rechte geben (Privilegien-Eskalation)."""
    return u.is_superadmin or u.has_permission("admin:roles")


def can_manage_accounts(u: AuthUser) -> bool:
    return u.is_superadmin or u.has_permission("admin:accounts")


def can_manage_cards(u: AuthUser) -> bool:
    # users:write schliesst Karten ein (wer Mitarbeiter bearbeiten darf, darf
    # auch deren Badge setzen); admin:cards ist die schmale Variante fuer z.B. HR.
    return u.is_superadmin or u.has_permission("admin:cards") or u.has_permission("users:write")


def can_manage_overtime(u: AuthUser) -> bool:
    return (
        u.has_permission("overtime:manage")
        or u.has_permission("timesheet:edit_all")
        or u.has_permission_namespace("admin")
    )
