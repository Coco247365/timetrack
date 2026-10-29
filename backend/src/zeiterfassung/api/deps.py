"""FastAPI dependency providers: DB session, current user, permissions."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Annotated

import jwt
from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from zeiterfassung.config import get_settings
from zeiterfassung.core.errors import ErrorCode, api_error
from zeiterfassung.core.security import decode_access_token

settings = get_settings()

# ---------------------------------------------------------------------------
# Database engine and session factory
# ---------------------------------------------------------------------------

_engine = create_async_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    echo=settings.debug,
)

_AsyncSessionLocal = async_sessionmaker(
    _engine,
    expire_on_commit=False,
    class_=AsyncSession,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Yield an async DB session, rolling back on unhandled exception."""
    async with _AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


# ---------------------------------------------------------------------------
# ScopeContext — resolved data visibility scope
# ---------------------------------------------------------------------------

# Scope priority ordering (highest to lowest).
_SCOPE_PRIORITY: dict[str, int] = {
    "global": 4,
    "department": 3,
    "self": 2,
    "anonymized_global": 1,
}


def _highest_scope(scopes: list[str]) -> str:
    """Return the scope with the highest priority from the given list.

    Falls back to 'self' if the list is empty or contains unknown values.
    """
    if not scopes:
        return "self"
    return max(scopes, key=lambda s: _SCOPE_PRIORITY.get(s, 0), default="self")


@dataclass(frozen=True)
class ScopeContext:
    """Data visibility scope resolved from the user's role.

    Encapsulates what data a user may see — used by repository filters and
    response serializers to enforce data boundaries per ADR-007.
    """

    scope: str  # 'global' | 'department' | 'self' | 'anonymized_global'
    tenant_id: uuid.UUID
    user_id: uuid.UUID | None  # the linked employee id (None for accounts without employee)
    department_id: uuid.UUID | None = None
    # Alle zustaendigen Bereiche (>=1 Element, wenn department_id gesetzt ist).
    department_ids: tuple[uuid.UUID, ...] = ()

    @property
    def dept_ids(self) -> tuple[uuid.UUID, ...]:
        """Alle zustaendigen Bereiche; Fallback auf die einzelne Abteilung."""
        if self.department_ids:
            return self.department_ids
        return (self.department_id,) if self.department_id else ()

    @property
    def is_global(self) -> bool:
        """True when the user can see all tenant data (GF, HR)."""
        return self.scope == "global"

    @property
    def is_department(self) -> bool:
        """True when the user can see their department's data (Teamleiter)."""
        return self.scope == "department"

    @property
    def is_self_only(self) -> bool:
        """True when the user can only see their own data (Mitarbeiter)."""
        return self.scope == "self"

    @property
    def is_anonymized(self) -> bool:
        """True when the user sees only anonymized aggregates (Betriebsrat)."""
        return self.scope == "anonymized_global"


# ---------------------------------------------------------------------------
# Current user extraction from Bearer token
# ---------------------------------------------------------------------------


class CurrentUser:
    """Lightweight auth principal parsed from JWT claims."""

    def __init__(
        self,
        user_id: uuid.UUID | None,
        tenant_id: uuid.UUID,
        permissions: list[str],
        department_id: uuid.UUID | None = None,
        role_type: str | None = None,
        data_scope: str = "self",
        account_id: uuid.UUID | None = None,
        employee_id: uuid.UUID | None = None,
        is_superadmin: bool = False,
        department_ids: list[uuid.UUID] | None = None,
    ) -> None:
        # user_id = the linked employee id (legacy name; used for "self" scope and
        # as the actor in audit/decision fields). None for accounts without employee.
        self.user_id = user_id
        self.tenant_id = tenant_id
        self.permissions = permissions
        self.department_id = department_id
        # Alle zustaendigen Bereiche; Fallback: die eine Abteilung (Alt-Tokens).
        self.department_ids: list[uuid.UUID] = (
            department_ids if department_ids else ([department_id] if department_id else [])
        )
        self.role_type = role_type
        self.data_scope = data_scope
        self.account_id = account_id
        self.employee_id = employee_id
        self.is_superadmin = is_superadmin

    def has_permission(self, perm: str) -> bool:
        """Exact permission check. Use has_permission_namespace for broad checks."""
        return perm in self.permissions

    def has_permission_namespace(self, namespace: str) -> bool:
        """Namespace match: 'admin' matches any 'admin:*' permission."""
        return any(p == namespace or p.startswith(namespace + ":") for p in self.permissions)

    def get_scope(self) -> ScopeContext:
        """Return a frozen ScopeContext derived from this user's claims."""
        return ScopeContext(
            scope=self.data_scope,
            tenant_id=self.tenant_id,
            user_id=self.user_id,
            department_id=self.department_id,
            department_ids=tuple(self.department_ids),
        )


def _extract_bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return authorization[7:]


async def get_current_user(
    db: Annotated[AsyncSession, Depends(get_db)],
    authorization: Annotated[str | None, Header()] = None,
) -> CurrentUser:
    """Extract and validate the JWT from the Authorization header.

    Login-hardening (Spec 2026-07-20): zusaetzlich zum JWT-Decode ein
    PK-Lookup auf accounts — Tokens geloeschter/deaktivierter Accounts und
    Tokens mit alter token_version (ver-Claim) werden SOFORT abgelehnt statt
    erst nach Token-Ablauf. Fehlender ver-Claim gilt als 0 (Alt-Tokens beim
    Rollout). Generische 401-Meldung — kein Orakel, warum abgelehnt wurde.
    """
    token = _extract_bearer(authorization)
    try:
        payload = decode_access_token(token)
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expired",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except jwt.InvalidTokenError as exc:
        raise api_error(
            status.HTTP_401_UNAUTHORIZED,
            exc,
            code=ErrorCode.INVALID_TOKEN,
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    from zeiterfassung.models.account import Account

    try:
        account_uuid = uuid.UUID(str(payload["sub"]))
    except (KeyError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    account = (
        await db.execute(select(Account).where(Account.id == account_uuid))
    ).scalar_one_or_none()
    token_ver = int(payload.get("ver") or 0)
    if account is None or not account.is_active or token_ver != (account.token_version or 0):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Parse department_id — may be absent in tokens issued before 0002 migration.
    raw_dept = payload.get("department_id")
    department_id: uuid.UUID | None = None
    if raw_dept:
        try:
            department_id = uuid.UUID(raw_dept)
        except (ValueError, AttributeError):
            department_id = None

    # sub = account id; employee_id links to the person (None for admin accounts).
    raw_emp = payload.get("employee_id")
    employee_id: uuid.UUID | None = None
    if raw_emp:
        try:
            employee_id = uuid.UUID(raw_emp)
        except (ValueError, AttributeError):
            employee_id = None

    # Mehrere zustaendige Bereiche (Claim ist optional — Alt-Tokens haben nur
    # department_id, dann greift der Fallback im CurrentUser-Konstruktor).
    department_ids: list[uuid.UUID] = []
    for raw in payload.get("department_ids") or []:
        try:
            department_ids.append(uuid.UUID(str(raw)))
        except (ValueError, AttributeError):
            continue

    return CurrentUser(
        user_id=employee_id,  # legacy field = linked employee (for self-scope/actor)
        tenant_id=uuid.UUID(payload["tenant_id"]),
        permissions=payload.get("permissions", []),
        department_id=department_id,
        department_ids=department_ids or None,
        role_type=payload.get("role_type"),
        data_scope=payload.get("data_scope", "self"),
        account_id=uuid.UUID(payload["sub"]),
        employee_id=employee_id,
        is_superadmin=bool(payload.get("is_superadmin", False)),
    )


async def get_current_admin(
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> CurrentUser:
    """Raise 403 if the caller does not have the 'admin' permission."""
    if not current_user.has_permission_namespace("admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin permission required",
        )
    return current_user


# ---------------------------------------------------------------------------
# Medical data access guard
# ---------------------------------------------------------------------------


async def require_medical_access(
    request: Request,
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> CurrentUser:
    """Dependency that enforces medical:read permission and a reason header.

    Usage::

        @router.get("/sick-leave/{id}/details")
        async def get_sick_leave_details(
            user: Annotated[CurrentUser, Depends(require_medical_access)],
            ...
        ):
            ...

    The caller must supply an ``X-Access-Reason`` header with at least 3
    non-whitespace characters explaining why they are accessing the data.
    This reason is written to the audit log by the endpoint implementation.
    """
    if "medical:read" not in current_user.permissions:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Medical data access requires medical:read permission",
        )
    reason = request.headers.get("X-Access-Reason", "")
    if len(reason.strip()) < 3:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="X-Access-Reason header required for medical data access",
        )
    return current_user


# ---------------------------------------------------------------------------
# NFC terminal device authentication
# ---------------------------------------------------------------------------


def hash_device_key(key: str) -> str:
    """Return the sha256 hex digest of a device API key."""
    return hashlib.sha256(key.encode()).hexdigest()


@dataclass(frozen=True)
class DeviceContext:
    """Authenticated NFC terminal device resolved from DB."""

    device_id: str  # str(device.id) — UUID of the Device row
    tenant_id: uuid.UUID  # tenant the device belongs to
    name: str  # human-readable device name


async def get_device(
    db: Annotated[AsyncSession, Depends(get_db)],
    x_device_api_key: Annotated[str | None, Header(alias="X-Device-Api-Key")] = None,
) -> DeviceContext:
    """Validate an NFC terminal API key against the devices table.

    - 401 if the header is missing or empty.
    - 401 if no active device record matches the key hash.
    - Updates ``last_seen_at`` on every successful authentication.
    """
    from zeiterfassung.models.base import utc_now
    from zeiterfassung.models.device import Device

    if not x_device_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Device API key required",
        )

    key_hash = hash_device_key(x_device_api_key)
    result = await db.execute(
        select(Device).where(Device.key_hash == key_hash, Device.is_active.is_(True))
    )
    device = result.scalar_one_or_none()
    if device is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid device key",
        )

    device.last_seen_at = utc_now()
    await db.flush()

    return DeviceContext(
        device_id=str(device.id),
        tenant_id=device.tenant_id,
        name=device.name,
    )


# ---------------------------------------------------------------------------
# Type aliases for use in route signatures
# ---------------------------------------------------------------------------

DBSession = Annotated[AsyncSession, Depends(get_db)]
AuthUser = Annotated[CurrentUser, Depends(get_current_user)]
AdminUser = Annotated[CurrentUser, Depends(get_current_admin)]
DeviceCtx = Annotated[DeviceContext, Depends(get_device)]
MedicalUser = Annotated[CurrentUser, Depends(require_medical_access)]
