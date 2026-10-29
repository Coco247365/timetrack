"""JWT token management and password hashing (argon2id)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError

from zeiterfassung.config import get_settings

_ph = PasswordHasher(
    time_cost=2,
    memory_cost=65536,  # 64 MiB
    parallelism=2,
    hash_len=32,
    salt_len=16,
)

settings = get_settings()


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------


def hash_password(plaintext: str) -> str:
    """Return argon2id hash of *plaintext*."""
    return _ph.hash(plaintext)


def verify_password(plaintext: str, hashed: str) -> bool:
    """Return True if *plaintext* matches *hashed*, False otherwise."""
    try:
        return _ph.verify(hashed, plaintext)
    except (VerifyMismatchError, VerificationError):
        return False


def password_needs_rehash(hashed: str) -> bool:
    """Return True if the hash should be upgraded (argon2 parameters changed)."""
    return _ph.check_needs_rehash(hashed)


# ---------------------------------------------------------------------------
# JWT tokens
# ---------------------------------------------------------------------------

TOKEN_TYPE_ACCESS = "access"
TOKEN_TYPE_REFRESH = "refresh"

# aud/iss (fkw.4, Phase 6): bindet Tokens an dieses Deployment/diese API — ein
# Token, das anderswo (z. B. versehentlich mit demselben Secret) ausgestellt
# wurde, wird nicht akzeptiert. Aendert man diese Werte, werden alle bestehenden
# Tokens beim naechsten Deploy ungueltig (akzeptiert, kleine Pilotgruppe, siehe
# docs/plan-jwt-login-haertung.md Phase 6).
JWT_ISSUER = "zeiterfassung"
JWT_AUDIENCE = "zeiterfassung-api"


def _create_token(
    subject: str,
    token_type: str,
    extra_claims: dict[str, Any] | None = None,
    expires_delta: timedelta | None = None,
) -> str:
    now = datetime.now(tz=UTC)
    if expires_delta is None:
        expires_delta = timedelta(minutes=settings.jwt_access_token_expire_minutes)
    payload: dict[str, Any] = {
        "sub": subject,
        "type": token_type,
        "iat": now,
        "exp": now + expires_delta,
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
    }
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def create_access_token(
    user_id: str,
    tenant_id: str,
    permissions: list[str] | None = None,
    department_id: str | None = None,
    role_type: str | None = None,
    data_scope: str = "self",
    employee_id: str | None = None,
    is_superadmin: bool = False,
    department_ids: list[str] | None = None,
    token_version: int = 0,
) -> str:
    """Create a short-lived JWT access token.

    Args:
        user_id: UUID string of the authenticated user.
        tenant_id: UUID string of the user's tenant.
        permissions: List of permission strings from the user's roles.
        department_id: UUID string of the user's department (may be None).
        role_type: Canonical role type slug (e.g. 'teamleiter', 'hr').
        data_scope: Highest data_scope across the user's roles. Defaults to
            'self' for backward compatibility with tokens issued before
            migration 0002.
    """
    extra: dict[str, Any] = {
        "tenant_id": tenant_id,
        "permissions": permissions or [],
        "data_scope": data_scope,
        "is_superadmin": is_superadmin,
        # Sofortige Entwertung (login-hardening): muss zur accounts.token_version
        # passen, sonst lehnt get_current_user den Token ab.
        "ver": token_version,
    }
    if department_id is not None:
        extra["department_id"] = department_id
    if department_ids:
        extra["department_ids"] = department_ids
    if role_type is not None:
        extra["role_type"] = role_type
    if employee_id is not None:
        extra["employee_id"] = employee_id
    return _create_token(
        subject=user_id,
        token_type=TOKEN_TYPE_ACCESS,
        extra_claims=extra,
        expires_delta=timedelta(minutes=settings.jwt_access_token_expire_minutes),
    )


def create_refresh_token(user_id: str, tenant_id: str, jti: str | None = None) -> str:
    """Create a long-lived JWT refresh token.

    ``jti`` identifies the issued token 1:1 with a ``refresh_sessions`` row
    (Rotation/Reuse-Detection, Bead fkw.2). Optional for backward compatibility
    with tokens issued before that migration — a refresh token without a
    ``jti`` claim has no matching session row and is rejected on refresh.
    """
    extra: dict[str, Any] = {"tenant_id": tenant_id}
    if jti is not None:
        extra["jti"] = jti
    return _create_token(
        subject=user_id,
        token_type=TOKEN_TYPE_REFRESH,
        extra_claims=extra,
        expires_delta=timedelta(days=settings.jwt_refresh_token_expire_days),
    )


def decode_token(token: str) -> dict[str, Any]:
    """Decode and validate a JWT, raising jwt.InvalidTokenError on failure.

    Validates ``aud``/``iss`` in addition to signature/expiry (fkw.4) — a token
    without matching claims (e.g. issued before this change, or by another
    deployment sharing a secret) is rejected.
    """
    return jwt.decode(
        token,
        settings.jwt_secret,
        algorithms=[settings.jwt_algorithm],
        audience=JWT_AUDIENCE,
        issuer=JWT_ISSUER,
    )


def decode_access_token(token: str) -> dict[str, Any]:
    """Decode an access token and verify its type."""
    payload = decode_token(token)
    if payload.get("type") != TOKEN_TYPE_ACCESS:
        raise jwt.InvalidTokenError("Not an access token")
    return payload


def decode_refresh_token(token: str) -> dict[str, Any]:
    """Decode a refresh token and verify its type."""
    payload = decode_token(token)
    if payload.get("type") != TOKEN_TYPE_REFRESH:
        raise jwt.InvalidTokenError("Not a refresh token")
    return payload
