"""User management endpoints: CRUD + own-profile + DSGVO export + /auth/me.

Split out of the former monolithic ``api/v1/users.py`` (zeiterfassung-6pcv).
Mounted at the same absolute paths as before (this router carries no prefix —
see ``api/v1/users/__init__.py``).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status

from zeiterfassung.api.deps import AuthUser, DBSession
from zeiterfassung.api.v1.users._permissions import can_manage_cards
from zeiterfassung.core.errors import api_error
from zeiterfassung.schemas.user import (
    AuthMeResponse,
    UserCreate,
    UserResponse,
    UserUpdate,
)
from zeiterfassung.services import user_service

router = APIRouter()


@router.post("/users", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(
    body: UserCreate,
    db: DBSession,
    current_user: AuthUser,
) -> UserResponse:
    """Create a new user.

    Requires ``users:write`` permission (GF/HR) or legacy ``admin``.
    """
    if not current_user.has_permission("users:write"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="users:write permission required",
        )
    try:
        user = await user_service.create_user(
            db, current_user.tenant_id, body, actor_id=current_user.user_id
        )
    except user_service.DuplicateEmailError as exc:
        raise api_error(status.HTTP_409_CONFLICT, exc) from exc
    except user_service.DepartmentCompanyMismatchError as exc:
        raise api_error(status.HTTP_422_UNPROCESSABLE_ENTITY, exc) from exc
    except user_service.RoleNotFoundError as exc:
        raise api_error(status.HTTP_404_NOT_FOUND, exc) from exc
    await db.commit()
    return UserResponse.model_validate(user)


@router.get("/users", response_model=list[UserResponse])
async def list_users(
    db: DBSession,
    current_user: AuthUser,
    skip: int = 0,
    limit: int = 100,
    active_only: bool = True,
) -> list[UserResponse]:
    """List users filtered by the caller's data visibility scope.

    - ``users:read_all`` (GF/HR): all tenant users
    - ``users:read_team`` (Teamleiter): users in same department
    - ``users:read_own`` (all roles): only own profile
    - Anonymized (Betriebsrat): empty list
    - Legacy ``admin`` permission: all users
    """
    scope = current_user.get_scope()
    users = await user_service.list_users(
        db, current_user.tenant_id, skip, limit, active_only, scope=scope
    )
    return [UserResponse.model_validate(u) for u in users]


@router.get("/users/me", response_model=UserResponse)
async def get_current_user_profile(
    db: DBSession,
    current_user: AuthUser,
) -> UserResponse:
    """Return the authenticated user's own profile."""
    try:
        user = await user_service.get_user_by_id(db, current_user.tenant_id, current_user.user_id)
    except user_service.UserNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found") from exc
    return UserResponse.model_validate(user)


@router.get("/users/me/export", response_model=dict)
async def export_own_data(
    db: DBSession,
    current_user: AuthUser,
) -> dict:
    """DSGVO Art. 15 — export all personal data for the authenticated user.

    Returns a JSON object containing all stored data about the caller:
    - user profile
    - punch events
    - daily timesheets
    - absence requests
    - approval audit log entries (as requester)

    Available to every authenticated user (``export:own_data`` is granted to all
    roles in the permission matrix).
    """
    try:
        return await user_service.export_own_data(db, current_user.tenant_id, current_user.user_id)
    except user_service.UserNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found") from exc


@router.get("/auth/me", response_model=AuthMeResponse, tags=["auth"])
async def get_auth_me(
    db: DBSession,
    current_user: AuthUser,
) -> AuthMeResponse:
    """Return user profile + permissions + scope claims from JWT.

    Includes department_id, department_name, role_type, and data_scope so the
    frontend can adapt navigation without an extra request.
    """
    data = await user_service.get_auth_me_data(db, current_user)
    return AuthMeResponse(**data)


@router.get("/users/{user_id}", response_model=UserResponse)
async def get_user(
    user_id: uuid.UUID,
    db: DBSession,
    current_user: AuthUser,
) -> UserResponse:
    """Get a user by ID.

    Access rules:
    - Own ID: always allowed
    - ``users:read_all`` (GF/HR) or legacy ``admin``: any tenant user
    - ``users:read_team`` (Teamleiter): only users in same department
    - Self-only / Anonymized: own user only
    """
    is_own = user_id == current_user.user_id
    has_read_all = current_user.has_permission("users:read_all") or current_user.has_permission(
        "admin"
    )
    has_read_team = current_user.has_permission("users:read_team")

    try:
        user = await user_service.get_user_with_department_scope_check(
            db,
            current_user.tenant_id,
            user_id,
            is_own=is_own,
            has_read_all=has_read_all,
            has_read_team=has_read_team,
            caller_department_ids=current_user.department_ids,
        )
    except user_service.UserNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found") from exc
    return UserResponse.model_validate(user)


@router.patch("/users/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: uuid.UUID,
    body: UserUpdate,
    db: DBSession,
    current_user: AuthUser,
) -> UserResponse:
    """Update a user.

    Requires ``users:write`` permission (GF/HR) or legacy ``admin``.
    """
    if not current_user.has_permission("users:write") and not current_user.is_superadmin:
        # Ausnahme: reine Badge-Zuweisung (nfc_badge_id) ist mit admin:cards
        # erlaubt — so kann z.B. HR Karten verwalten, ohne Mitarbeiterdaten
        # bearbeiten zu duerfen.
        provided = set(body.model_dump(exclude_unset=True))
        badge_only = provided <= {"nfc_badge_id"}
        if not (badge_only and can_manage_cards(current_user)):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="users:write permission required",
            )
    try:
        user = await user_service.update_user(
            db, current_user.tenant_id, user_id, body, actor_id=current_user.user_id
        )
    except user_service.UserNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found") from exc
    except user_service.DepartmentCompanyMismatchError as exc:
        raise api_error(status.HTTP_422_UNPROCESSABLE_ENTITY, exc) from exc
    except user_service.WorkTimeProfileMismatchError as exc:
        raise api_error(status.HTTP_422_UNPROCESSABLE_ENTITY, exc) from exc
    except user_service.RoleNotFoundError as exc:
        raise api_error(status.HTTP_404_NOT_FOUND, exc) from exc
    await db.commit()
    return UserResponse.model_validate(user)


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: uuid.UUID,
    db: DBSession,
    current_user: AuthUser,
) -> None:
    """Einen soft-geloeschten Mitarbeiter endgueltig entfernen.

    Requires ``users:write`` (oder Superadmin). Nur erlaubt, wenn der
    Mitarbeiter bereits ``employment_status == "deleted"`` ist und keinerlei
    Zeit-/Lohndaten hat (sonst 409) — echte Austritte laufen ueber das Archiv.
    """
    if not current_user.has_permission("users:write") and not current_user.is_superadmin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="users:write permission required",
        )
    try:
        await user_service.delete_user_hard(
            db, current_user.tenant_id, user_id, actor_id=current_user.user_id
        )
    except user_service.UserNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found") from exc
    except (user_service.UserNotDeletedError, user_service.UserHasHistoryError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await db.commit()
