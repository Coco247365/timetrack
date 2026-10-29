"""Application configuration via Pydantic Settings."""

from __future__ import annotations

from datetime import date
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ZEITERFASSUNG_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Database
    database_url: str = "postgresql+asyncpg://localhost/zeiterfassung"

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # JWT
    jwt_secret: str = "CHANGE_ME_IN_PRODUCTION_USE_A_LONG_RANDOM_STRING"
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 15
    jwt_refresh_token_expire_days: int = 30
    # httpOnly-Refresh-Cookie: Secure-Flag (nur per HTTPS gesendet). Default an;
    # nur fuer lokale Entwicklung ohne TLS auf false setzen
    # (ZEITERFASSUNG_AUTH_COOKIE_SECURE=false).
    auth_cookie_secure: bool = True

    # Krankmeldung per E-Mail (Microsoft 365 / Graph). Ohne Werte: deaktiviert.
    m365_tenant_id: str | None = None
    m365_client_id: str | None = None
    m365_client_secret: str | None = None
    m365_mailbox: str | None = None  # z. B. krank@example.com
    sickmail_poll_seconds: int = 300

    # App defaults
    default_timezone: str = "Europe/Berlin"
    default_bundesland: str = "HH"  # federal state code (overridable per tenant)

    # Lohnlauf-Stichtag: Monate VOR diesem Datum wurden noch ueber das
    # Altsystem abgerechnet. Ein (Neu-)Abschluss solcher
    # Monate erzeugt dann KEINE PayrollBooking — sonst wuerden bereits
    # bezahlte Ueberstunden ein zweites Mal in den DATEV-Export laufen.
    # None = kein Stichtag (Default, u. a. fuer Tests).
    payroll_cutoff_date: date | None = None

    # CORS — Prod laeuft same-origin hinter Caddy (Frontend + Backend selbe
    # Origin), braucht also gar kein CORS. Dieser Default deckt nur lokale
    # Dev-Setups (Nuxt-Devserver auf :3000) ab; ueber die Env-Var breiter
    # ziehen, falls doch mal Cross-Origin-Zugriff noetig wird (fkw.4/Phase 6).
    cors_origins: list[str] = ["http://localhost:3000", "http://127.0.0.1:3000"]

    # Hatchet workflow engine
    hatchet_token: str = ""
    hatchet_host_port: str = "localhost:7077"
    hatchet_server_url: str = "http://localhost:7078"
    hatchet_tls_strategy: str = "none"

    # DATEV LODAS export
    datev_berater_nr: str = ""  # Steuerberater-Nr (e.g. "12345")
    datev_mandanten_nr: str = ""  # Mandanten-Nr (e.g. "54321")
    datev_daily_hours: float = 7.7  # Daily hours for absence→hours conversion (154h/20d)

    # Demo: auto-assign an unknown NFC card to the next badge-less employee on
    # first tap (terminal shows a real name instead of an error). Never in prod.
    demo_autoassign: bool = False

    # NFC-Endpunkt-Härtung (in-process, siehe api/v1/clock.py). Schützt vor
    # Badge-ID-Enumeration und dem is_admin-Oracle auf /nfc/preview und
    # /nfc/profile. Nur pro Prozess gültig — ein Mehrprozess-Deployment
    # (mehrere uvicorn-Worker) bräuchte gemeinsamen Zustand (z. B. Redis);
    # aktuell außerhalb des Scopes.
    nfc_rate_limit_max_requests: int = 120
    nfc_rate_limit_max_failures: int = 20
    nfc_rate_limit_window_seconds: int = 60
    # Obergrenze für tenant.settings["scanned_cards"] (unbekannte Badges) —
    # verhindert unbegrenztes Wachstum der Tenant-JSONB-Spalte.
    nfc_scanned_cards_max: int = 500

    # Automatisches Ausstempeln (Spec 2026-08-03): eine Schicht, die länger als
    # auto_clockout_hours offen ist, gilt als „Ausstempeln vergessen" und wird
    # bei Schichtbeginn + auto_clockout_hours automatisch geschlossen. Ein
    # Sweep-Job prüft alle auto_clockout_sweep_minutes. Normale Nachtschichten
    # (< 14 h) bleiben unberührt.
    auto_clockout_hours: int = 14
    auto_clockout_sweep_minutes: int = 15

    # Auth-Rate-Limiting (login-hardening, Spec 2026-07-20): in-process
    # Sliding-Window auf /auth/login (pro Client-IP und pro E-Mail) und
    # /auth/refresh (pro Client-IP). Ergaenzt den DB-Lockout (10 Fehlversuche
    # -> 5 min) um Schutz vor verteiltem Raten ueber viele Accounts. Wie die
    # nfc_*-Limits pro Prozess gueltig. Hinter Caddy braucht uvicorn
    # --proxy-headers, sonst ist die Client-IP immer 127.0.0.1 und das
    # IP-Limit wirkt global (siehe Spec, Deploy-Voraussetzung).
    auth_rate_limit_login_ip_max: int = 10
    auth_rate_limit_login_email_max: int = 5
    auth_rate_limit_refresh_ip_max: int = 30
    auth_rate_limit_window_seconds: int = 60

    # Environment
    debug: bool = False
    environment: str = "development"

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, v: str) -> str:
        if not v.startswith("postgresql"):
            raise ValueError("database_url must be a PostgreSQL URL")
        return v


@lru_cache
def get_settings() -> Settings:
    """Return cached settings instance."""
    return Settings()
