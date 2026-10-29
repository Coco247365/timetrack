"""Administration tool: full employee view + org structure (departments,
companies, roles, login accounts, scanned NFC cards, org chart).

Split out of the former monolithic ``api/v1/users.py`` (zeiterfassung-6pcv).
Mounted at the same absolute paths as before (this router carries no prefix —
see ``api/v1/users/__init__.py``); several of these paths are literally
``/admin/...`` even though this module isn't under the ``admin`` router
(that "/admin" prefix trick predates this split — kept as-is).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status

from zeiterfassung.api.deps import AuthUser, DBSession
from zeiterfassung.api.v1.users._permissions import (
    admin_can_read,
    admin_can_write,
    can_manage_accounts,
    can_manage_cards,
    can_manage_roles,
)
from zeiterfassung.services import org_service

router = APIRouter()


@router.get("/admin/scanned-cards", response_model=list[dict])
async def scanned_cards(db: DBSession, current_user: AuthUser) -> list[dict]:
    """Pool of scanned unknown NFC cards, excluding cards already assigned to an
    active user (those are "used")."""
    if not admin_can_read(current_user):
        raise HTTPException(status_code=403, detail="Admin read permission required")
    return await org_service.list_scanned_cards(db, current_user.tenant_id)


@router.post("/admin/scanned-cards/remove", response_model=dict)
async def remove_scanned_card(body: dict, db: DBSession, current_user: AuthUser) -> dict:
    """Remove a UID from the scanned-cards pool. Requires card management rights."""
    if not can_manage_cards(current_user):
        raise HTTPException(status_code=403, detail="admin:cards permission required")
    return await org_service.remove_scanned_card(db, current_user.tenant_id, body.get("uid"))


@router.get("/admin/employees", response_model=list[dict])
async def admin_employees(db: DBSession, current_user: AuthUser) -> list[dict]:
    """Full per-employee admin view (joins department, role, manager).

    Zugriff:
    - voll: users:read_all / users:write / admin-Namespace / reports:view
    - team-beschraenkt: users:read_team oder timesheet:view_team -> nur eigener Bereich
    """
    full_access = admin_can_read(current_user) or current_user.has_permission("reports:view")
    team_access = current_user.has_permission("users:read_team") or current_user.has_permission(
        "timesheet:view_team"
    )
    if not full_access and not team_access:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Admin read permission required"
        )
    return await org_service.list_admin_employees(
        db,
        current_user.tenant_id,
        full_access=full_access,
        caller_department_ids=current_user.department_ids,
        caller_user_id=current_user.user_id,
    )


@router.post("/departments", response_model=dict, status_code=status.HTTP_201_CREATED)
async def create_department(body: dict, db: DBSession, current_user: AuthUser) -> dict:
    """Create a new department for the caller's tenant. Admin write only."""
    if not admin_can_write(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin:settings or users:write permission required",
        )
    return await org_service.create_department(db, current_user.tenant_id, body)


@router.delete("/departments/{department_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_department(department_id: str, db: DBSession, current_user: AuthUser) -> None:
    """Delete a department (admin write). Detaches employees first (department_id → null)."""
    if not admin_can_write(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin:settings or users:write permission required",
        )
    await org_service.delete_department(db, current_user.tenant_id, department_id)


@router.get("/companies", response_model=list[dict])
async def list_companies(db: DBSession, current_user: AuthUser) -> list[dict]:
    """List companies for the tenant (admin read)."""
    if not admin_can_read(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Admin read permission required"
        )
    return await org_service.list_companies(db, current_user.tenant_id)


@router.post("/companies", response_model=dict, status_code=status.HTTP_201_CREATED)
async def create_company(body: dict, db: DBSession, current_user: AuthUser) -> dict:
    """Create a company. Admin write only."""
    if not admin_can_write(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin:settings or users:write permission required",
        )
    return await org_service.create_company(db, current_user.tenant_id, body)


@router.patch("/companies/{company_id}", response_model=dict)
async def update_company(
    company_id: uuid.UUID, body: dict, db: DBSession, current_user: AuthUser
) -> dict:
    """Rename a company. Admin write only."""
    if not admin_can_write(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin:settings or users:write permission required",
        )
    return await org_service.update_company(db, current_user.tenant_id, company_id, body)


@router.delete("/companies/{company_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_company(company_id: uuid.UUID, db: DBSession, current_user: AuthUser) -> None:
    """Delete a company. Employees keep existing — their company_id is set NULL via FK."""
    if not admin_can_write(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin:settings or users:write permission required",
        )
    await org_service.delete_company(db, current_user.tenant_id, company_id)


@router.get("/departments", response_model=list[dict])
async def list_departments(
    db: DBSession, current_user: AuthUser, company_id: uuid.UUID | None = None
) -> list[dict]:
    """List departments (incl. team lead) for the admin dropdowns."""
    if not admin_can_read(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Admin read permission required"
        )
    return await org_service.list_departments(db, current_user.tenant_id, company_id)


@router.post("/roles", response_model=dict, status_code=status.HTTP_201_CREATED)
async def create_role(body: dict, db: DBSession, current_user: AuthUser) -> dict:
    """Create a new role for the caller's tenant. Admin write only."""
    if not can_manage_roles(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin:roles permission required",
        )
    return await org_service.create_role(db, current_user.tenant_id, body)


@router.get("/roles", response_model=list[dict])
async def list_roles(db: DBSession, current_user: AuthUser) -> list[dict]:
    """List roles (status levels) for the admin dropdowns."""
    if not admin_can_read(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Admin read permission required"
        )
    return await org_service.list_roles(db, current_user.tenant_id)


@router.patch("/roles/{role_id}", response_model=dict)
async def update_role(
    role_id: uuid.UUID,
    body: dict,
    db: DBSession,
    current_user: AuthUser,
) -> dict:
    """Update a role. Body fields all optional: name, role_type, data_scope, permissions."""
    if not can_manage_roles(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin:roles permission required",
        )
    return await org_service.update_role(db, current_user.tenant_id, role_id, body)


@router.delete("/roles/{role_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_role(role_id: uuid.UUID, db: DBSession, current_user: AuthUser) -> None:
    """Delete a role (admin write). Account/employee assignments cascade away."""
    if not can_manage_roles(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin:roles permission required",
        )
    await org_service.delete_role(db, current_user.tenant_id, role_id)


@router.get("/accounts", response_model=list[dict])
async def list_accounts(db: DBSession, current_user: AuthUser) -> list[dict]:
    """List login accounts for the tenant (admin read)."""
    if not admin_can_read(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Admin read permission required"
        )
    return await org_service.list_accounts(db, current_user.tenant_id)


@router.post("/accounts", response_model=dict, status_code=status.HTTP_201_CREATED)
async def create_account(body: dict, db: DBSession, current_user: AuthUser) -> dict:
    """Create a login account.

    Body: email, password, role_id, optional employee_id/is_superadmin.
    """
    if not can_manage_accounts(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin:accounts permission required",
        )
    return await org_service.create_account(
        db,
        current_user.tenant_id,
        body,
        actor_is_superadmin=current_user.is_superadmin,
        actor_can_manage_roles=can_manage_roles(current_user),
    )


@router.patch("/accounts/{account_id}", response_model=dict)
async def update_account(
    account_id: uuid.UUID, body: dict, db: DBSession, current_user: AuthUser
) -> dict:
    """Update an account: role_id, is_active, employee_id, is_superadmin, password."""
    if not can_manage_accounts(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin:accounts permission required",
        )
    return await org_service.update_account(
        db,
        current_user.tenant_id,
        account_id,
        body,
        actor_account_id=current_user.account_id,
        actor_is_superadmin=current_user.is_superadmin,
        actor_can_manage_roles=can_manage_roles(current_user),
    )


@router.post("/accounts/{account_id}/reset-password", response_model=dict)
async def reset_account_password(
    account_id: uuid.UUID, db: DBSession, current_user: AuthUser
) -> dict:
    """Admin setzt das Passwort eines Accounts auf das Start-Passwort zurück.

    Gibt das Start-Passwort zurück, damit der Admin es weitergeben kann.
    """
    if not can_manage_accounts(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin:accounts permission required",
        )
    return await org_service.reset_account_password(
        db, current_user.tenant_id, account_id, actor_is_superadmin=current_user.is_superadmin
    )


@router.delete("/accounts/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(account_id: uuid.UUID, db: DBSession, current_user: AuthUser) -> None:
    """Login-Konto endgueltig loeschen (admin:accounts). Der verknuepfte
    Mitarbeiter und seine Zeitdaten bleiben erhalten (Login != Mitarbeiter).

    Sperren: das eigene Konto sowie der letzte aktive Superadmin sind nicht
    loeschbar.
    """
    if not can_manage_accounts(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="admin:accounts permission required"
        )
    await org_service.delete_account(
        db,
        current_user.tenant_id,
        account_id,
        actor_account_id=current_user.account_id,
        actor_employee_id=current_user.employee_id,
    )


@router.patch("/departments/{department_id}", response_model=dict)
async def set_department_lead(
    department_id: uuid.UUID,
    body: dict,
    db: DBSession,
    current_user: AuthUser,
) -> dict:
    """Update a department: team lead, name and/or code. Admin write only.

    Body fields are all optional: ``name``, ``code``, ``teamleiter_user_id``.
    """
    if not admin_can_write(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin:settings or users:write permission required",
        )
    return await org_service.set_department_lead(db, current_user.tenant_id, department_id, body)


@router.get("/org-chart", response_model=dict)
async def org_chart(db: DBSession, current_user: AuthUser) -> dict:
    """Schlankes Organigramm: Name, Bereich, Vorgesetzter — sonst nichts.

    Gleiche Sichtbarkeitslogik wie der Teamkalender: calendar:view_all (oder
    breite Rechte) = ganze Firma, calendar:view_team = eigener Bereich. So
    koennen auch einfache Mitarbeiter die Hierarchie sehen, ohne Zugriff auf
    Stammdaten, Zeiten oder Kontakte.
    """
    # Eigene Rechte (pro Rolle waehlbar) — bewusst NICHT an Kalender-/Zeiten-
    # Rechte gekoppelt, damit die Sichtbarkeit unabhaengig einstellbar ist.
    # Admin-Namespace sieht immer alles (sonst koennte man sich aussperren).
    view_all = current_user.has_permission(
        "orgchart:view_all"
    ) or current_user.has_permission_namespace("admin")
    view_team = current_user.has_permission("orgchart:view_team")
    return await org_service.org_chart(
        db,
        current_user.tenant_id,
        view_all=view_all,
        view_team=view_team,
        caller_department_ids=current_user.department_ids,
        caller_user_id=current_user.user_id,
    )
