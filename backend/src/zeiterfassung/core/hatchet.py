"""Hatchet workflow engine client singleton.

The Hatchet Python SDK reads its configuration from environment variables
with the ``HATCHET_CLIENT_`` prefix:

- ``HATCHET_CLIENT_TOKEN``          — JWT issued by the Hatchet dashboard
- ``HATCHET_CLIENT_HOST_PORT``      — gRPC address (default: localhost:7077)
- ``HATCHET_CLIENT_SERVER_URL``     — REST/dashboard URL
- ``HATCHET_CLIENT_TLS_STRATEGY``   — "none" for local dev, "tls" for prod

This module exposes a lazy singleton so all application code shares a single
``Hatchet`` instance.  Lifecycle is managed by the FastAPI lifespan handler in
``zeiterfassung.main``.
"""

from __future__ import annotations

import logging
import os

from hatchet_sdk import ClientConfig, ClientTLSConfig, Hatchet

from zeiterfassung.config import get_settings

logger = logging.getLogger(__name__)

_hatchet_client: Hatchet | None = None


def _build_config() -> ClientConfig:
    """Build a ``ClientConfig`` from application settings.

    Settings values are applied only when the corresponding environment
    variable has not already been set, so operator-provided env vars always
    take precedence.
    """
    settings = get_settings()
    # Forward settings → env vars that the SDK reads natively.
    # We write only when the variable is absent so that explicit env overrides
    # (e.g. from Docker / Kubernetes) win.
    _setenv("HATCHET_CLIENT_TOKEN", settings.hatchet_token)
    _setenv("HATCHET_CLIENT_HOST_PORT", settings.hatchet_host_port)
    _setenv("HATCHET_CLIENT_SERVER_URL", settings.hatchet_server_url)
    _setenv("HATCHET_CLIENT_TLS_STRATEGY", settings.hatchet_tls_strategy)

    tls = ClientTLSConfig(strategy=settings.hatchet_tls_strategy)
    return ClientConfig(
        host_port=settings.hatchet_host_port,
        server_url=settings.hatchet_server_url,
        tls_config=tls,
    )


def _setenv(key: str, value: str) -> None:
    """Set an environment variable only when it is not already present."""
    if not os.environ.get(key) and value:
        os.environ[key] = value


def get_hatchet() -> Hatchet:
    """Return the shared ``Hatchet`` client singleton.

    Creates and caches the client on first call.  Raises ``RuntimeError`` when
    ``hatchet_token`` is empty and ``HATCHET_CLIENT_TOKEN`` is not set so
    misconfiguration surfaces early.

    Example::

        from zeiterfassung.core.hatchet import get_hatchet

        hatchet = get_hatchet()
        hatchet.event.push("clock.punched", {"employee_id": str(employee_id)})
    """
    global _hatchet_client
    if _hatchet_client is None:
        settings = get_settings()
        token = settings.hatchet_token or os.environ.get("HATCHET_CLIENT_TOKEN", "")
        if not token:
            raise RuntimeError(
                "Hatchet token is not configured. "
                "Set hatchet_token in .env or HATCHET_CLIENT_TOKEN env var."
            )
        config = _build_config()
        _hatchet_client = Hatchet(config=config)
        logger.info(
            "Hatchet client initialised — host_port=%s server_url=%s",
            config.host_port,
            config.server_url,
        )
    return _hatchet_client


def close_hatchet() -> None:
    """Release the Hatchet client on application shutdown.

    The Hatchet SDK does not expose an async close, so this is a plain
    synchronous function.
    """
    global _hatchet_client
    if _hatchet_client is not None:
        _hatchet_client = None
        logger.info("Hatchet client released")
