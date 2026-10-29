"""Security-Header auf allen API-Antworten (login-hardening, Spec Abschnitt 3)."""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from zeiterfassung.core.security_headers import _is_docs_path


@pytest_asyncio.fixture
async def client():
    from zeiterfassung.main import create_app

    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="https://test") as c:
        yield c


@pytest.mark.anyio
async def test_headers_present_on_regular_response(client) -> None:
    resp = await client.get("/health")
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    assert resp.headers["Content-Security-Policy"] == "default-src 'none'"


@pytest.mark.anyio
async def test_auth_responses_are_not_cacheable(client) -> None:
    # 401 reicht — die Middleware sitzt unter allem und greift auch da.
    resp = await client.post("/api/v1/auth/refresh")
    assert resp.headers["Cache-Control"] == "no-store"


@pytest.mark.anyio
async def test_error_responses_carry_headers_too(client) -> None:
    resp = await client.get("/api/v1/does-not-exist")
    assert resp.status_code == 404
    assert resp.headers["X-Content-Type-Options"] == "nosniff"


def test_docs_paths_are_exempt_from_csp() -> None:
    # /docs existiert nur bei DEBUG=true — hier reicht der Praedikat-Test.
    assert _is_docs_path("/docs")
    assert _is_docs_path("/docs/oauth2-redirect")
    assert _is_docs_path("/redoc")
    assert _is_docs_path("/openapi.json")
    assert not _is_docs_path("/api/v1/users")
    assert not _is_docs_path("/health")
