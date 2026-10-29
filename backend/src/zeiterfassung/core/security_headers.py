"""Security-Header fuer alle Backend-Antworten (login-hardening, Spec Abschnitt 3).

Reine ASGI-Middleware (kein BaseHTTPMiddleware — kein Response-Buffering).
Die API liefert nur JSON, darum ist die CSP maximal restriktiv
(default-src 'none'). Ausnahme: die Swagger-/Redoc-Seiten (nur bei
DEBUG=true ueberhaupt gemountet) laden Assets von CDN und rendern mit
dieser CSP nicht — sie bleiben ohne CSP, die uebrigen Header gelten auch dort.
Auth-Antworten (Tokens!) bekommen zusaetzlich Cache-Control: no-store.
"""

from __future__ import annotations

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_AUTH_PREFIX = "/api/v1/auth"


def _is_docs_path(path: str) -> bool:
    return path == "/openapi.json" or path.startswith("/docs") or path.startswith("/redoc")


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("X-Frame-Options", "DENY")
                headers.setdefault("Referrer-Policy", "no-referrer")
                if not _is_docs_path(path):
                    headers.setdefault("Content-Security-Policy", "default-src 'none'")
                if path.startswith(_AUTH_PREFIX):
                    headers["Cache-Control"] = "no-store"
            await send(message)

        await self.app(scope, receive, send_with_headers)
