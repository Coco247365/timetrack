"""Tests for the startup JWT-secret guard (Bead zeiterfassung-fkw.4, PR D / Phase 6).

Prod (non-debug) must refuse to start with the shipped CHANGE_ME default or a
short secret; debug mode only warns (Dev-Komfort ohne .env-Secret).
"""

from __future__ import annotations

import pytest

from zeiterfassung.config import Settings, get_settings
from zeiterfassung.main import create_app

_DEFAULT_SECRET = Settings.model_fields["jwt_secret"].default


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """``get_settings()`` is ``lru_cache``d — force re-read after monkeypatch."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_create_app_raises_on_default_secret_without_debug(monkeypatch) -> None:
    monkeypatch.setenv("ZEITERFASSUNG_JWT_SECRET", _DEFAULT_SECRET)
    monkeypatch.setenv("ZEITERFASSUNG_DEBUG", "false")
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        create_app()


def test_create_app_raises_on_short_secret_without_debug(monkeypatch) -> None:
    monkeypatch.setenv("ZEITERFASSUNG_JWT_SECRET", "short-secret")
    monkeypatch.setenv("ZEITERFASSUNG_DEBUG", "false")
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        create_app()


def test_create_app_only_warns_on_default_secret_in_debug(monkeypatch, caplog) -> None:
    monkeypatch.setenv("ZEITERFASSUNG_JWT_SECRET", _DEFAULT_SECRET)
    monkeypatch.setenv("ZEITERFASSUNG_DEBUG", "true")
    app = create_app()
    assert app is not None


def test_create_app_succeeds_with_a_real_secret(monkeypatch) -> None:
    monkeypatch.setenv("ZEITERFASSUNG_JWT_SECRET", "a-properly-long-random-secret-0123456789abcdef")
    monkeypatch.setenv("ZEITERFASSUNG_DEBUG", "false")
    app = create_app()
    assert app is not None
