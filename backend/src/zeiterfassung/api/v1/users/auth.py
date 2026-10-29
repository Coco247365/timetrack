"""Auth endpoints: login, refresh, logout, change-password.

Split out of the former monolithic ``api/v1/users.py`` (zeiterfassung-6pcv).
Mounted at the same absolute paths as before (this router carries no prefix —
see ``api/v1/users/__init__.py``).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse

from zeiterfassung.api.deps import AuthUser, DBSession
from zeiterfassung.config import get_settings
from zeiterfassung.core import rate_limit
from zeiterfassung.schemas.user import LoginRequest, TokenResponse
from zeiterfassung.services import user_service

router = APIRouter()


# httpOnly-Refresh-Cookie (fkw.2 / docs/plan-jwt-login-haertung.md Entscheidung 4).
# Nur an die Auth-Endpoints gesendet (Path-Scope) -> kein CSRF-Token noetig
# (SameSite=Strict + engster Cookie-Pfad + JSON-POST; Login selbst ist
# uncredentialed). Cookie-only seit fkw.4/Phase 6 — der Body-Fallback
# (Uebergang, fkw.2) ist entfernt; der Refresh-Token verlaesst den Server nie
# mehr im JSON-Body.
REFRESH_COOKIE_NAME = "zeit_refresh"
REFRESH_COOKIE_PATH = "/api/v1/auth"

# Generische 401-Meldung fuer die gesamte Auth-Oberflaeche (/auth/login*,
# /auth/refresh) — verhindert, dass ein Client aus der HTTP-Detail-Message
# ableiten kann, WARUM ein Versuch fehlschlug (falsches Passwort vs. Account
# gesperrt vs. Refresh-Token-Reuse vs. Tenant-Mismatch vs. ...). Die genaue
# Ursache bleibt intern (Exception-Typ, Audit-Log) erhalten — nur die
# HTTP-Antwort wird vereinheitlicht (Review-Finding aus PR #114, fkw.4).
GENERIC_AUTH_ERROR = "Could not validate credentials"


def _token_response(pair: user_service.TokenPair) -> TokenResponse:
    """Public JSON body: access_token/token_type/expires_in — NIE refresh_token."""
    return TokenResponse(access_token=pair.access_token, expires_in=pair.expires_in)


def _set_refresh_cookie(response: Response, refresh_token: str) -> None:
    s = get_settings()
    response.set_cookie(
        key=REFRESH_COOKIE_NAME,
        value=refresh_token,
        httponly=True,
        secure=s.auth_cookie_secure,
        samesite="strict",
        path=REFRESH_COOKIE_PATH,
        max_age=s.jwt_refresh_token_expire_days * 86400,
    )


def _delete_refresh_cookie(response: Response) -> None:
    response.delete_cookie(key=REFRESH_COOKIE_NAME, path=REFRESH_COOKIE_PATH)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _enforce_login_rate_limit(request: Request, email: str) -> None:
    """Sliding-Window pro Client-IP UND pro E-Mail (lowercase) — 429 mit
    generischer Meldung, bevor Credentials ueberhaupt geprueft werden."""
    s = get_settings()
    rate_limit.check(
        f"auth:login:ip:{_client_ip(request)}",
        s.auth_rate_limit_login_ip_max,
        s.auth_rate_limit_window_seconds,
    )
    rate_limit.check(
        f"auth:login:email:{email.strip().lower()}",
        s.auth_rate_limit_login_email_max,
        s.auth_rate_limit_window_seconds,
    )


@router.post("/auth/login", response_model=TokenResponse, tags=["auth"])
async def login(
    body: LoginRequest, db: DBSession, request: Request, response: Response
) -> TokenResponse:
    """Authenticate with email and password.

    Auto-resolves tenant when only one tenant exists (single-tenant mode).
    For multi-tenant, use POST /auth/login/{tenant_id}.
    """
    _enforce_login_rate_limit(request, body.email)
    tenant_id = await user_service.resolve_single_tenant(db)

    try:
        token_pair = await user_service.authenticate(
            db, tenant_id, body.email, body.password, user_agent=request.headers.get("user-agent")
        )
    except user_service.InvalidCredentialsError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=GENERIC_AUTH_ERROR,
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    _set_refresh_cookie(response, token_pair.refresh_token)
    return _token_response(token_pair)


@router.post("/auth/login/{tenant_id}", response_model=TokenResponse, tags=["auth"])
async def login_for_tenant(
    tenant_id: uuid.UUID,
    body: LoginRequest,
    db: DBSession,
    request: Request,
    response: Response,
) -> TokenResponse:
    """Authenticate a user within a specific tenant."""
    _enforce_login_rate_limit(request, body.email)
    try:
        token_pair = await user_service.authenticate(
            db, tenant_id, body.email, body.password, user_agent=request.headers.get("user-agent")
        )
    except user_service.InvalidCredentialsError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=GENERIC_AUTH_ERROR,
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    _set_refresh_cookie(response, token_pair.refresh_token)
    return _token_response(token_pair)


@router.post("/auth/refresh", tags=["auth"])
async def refresh_token(request: Request, db: DBSession) -> Response:
    """Exchange a refresh token for a new (rotated) token pair.

    Token-Quelle: ausschliesslich der httpOnly-Cookie (fkw.4 — der
    Body-Fallback aus fkw.2 ist entfernt). Erfolgreicher Refresh rotiert den
    Cookie; ein 401 loescht ihn (der Client soll ihn nicht mit einem
    ungueltig gewordenen Token weiterschicken).
    """
    s = get_settings()
    rate_limit.check(
        f"auth:refresh:ip:{_client_ip(request)}",
        s.auth_rate_limit_refresh_ip_max,
        s.auth_rate_limit_window_seconds,
    )
    token = request.cookies.get(REFRESH_COOKIE_NAME)
    if not token:
        resp = JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": GENERIC_AUTH_ERROR},
        )
        _delete_refresh_cookie(resp)
        return resp

    try:
        token_pair = await user_service.refresh_tokens(
            db, token, user_agent=request.headers.get("user-agent")
        )
    except user_service.InvalidCredentialsError:
        resp = JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": GENERIC_AUTH_ERROR},
        )
        _delete_refresh_cookie(resp)
        return resp

    resp = JSONResponse(content=_token_response(token_pair).model_dump(mode="json"))
    _set_refresh_cookie(resp, token_pair.refresh_token)
    return resp


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT, tags=["auth"])
async def logout(request: Request, response: Response, db: DBSession) -> None:
    """Refresh-Session (ganze Familie) revoken; Cookie wird immer geloescht.

    Kein Access-Token/``AuthUser`` mehr erforderlich (fkw.4, Review-Finding aus
    PR #114): der Refresh-Token (Cookie) IST das Credential — mit einem
    abgelaufenen Access-Token wuerde Logout sonst 401en, obwohl der
    Refresh-Cookie noch da ist und revoked werden koennte. Best-effort:
    fehlender/ungueltiger Refresh-Token fuehrt trotzdem zu 204 (Logout darf
    den Client nie blockieren).
    """
    token = request.cookies.get(REFRESH_COOKIE_NAME)
    if token:
        await user_service.revoke_refresh_session(db, token)
        await db.commit()
    _delete_refresh_cookie(response)


@router.post("/auth/change-password", status_code=status.HTTP_204_NO_CONTENT, tags=["auth"])
async def change_own_password(body: dict, db: DBSession, current_user: AuthUser) -> None:
    """Eigenes Account-Passwort ändern (aktuelles Passwort wird geprüft)."""
    await user_service.change_own_password(
        db,
        current_user.account_id,
        body.get("current_password") or "",
        body.get("new_password") or "",
    )
