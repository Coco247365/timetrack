"""FastAPI application factory."""

from __future__ import annotations

# --- BEGIN WIRE-OBSERVABILITY ---
import os as _os

_sentry_dsn = _os.environ.get("SENTRY_DSN", "")
if _sentry_dsn:
    try:
        import sentry_sdk

        sentry_sdk.init(
            dsn=_sentry_dsn,
            traces_sample_rate=0.1,
            environment=_os.environ.get("SENTRY_ENVIRONMENT", "production"),
        )
    except ImportError:
        import logging as _logging

        _logging.getLogger(__name__).warning(
            "SENTRY_DSN is set but sentry-sdk is not installed. Skipping."
        )
# --- END WIRE-OBSERVABILITY ---

from collections.abc import AsyncGenerator  # noqa: E402
from contextlib import asynccontextmanager  # noqa: E402

from fastapi import FastAPI  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

from zeiterfassung.api.v1.router import api_router  # noqa: E402
from zeiterfassung.config import Settings, get_settings  # noqa: E402
from zeiterfassung.core.security_headers import SecurityHeadersMiddleware  # noqa: E402

_JWT_SECRET_DEFAULT = Settings.model_fields["jwt_secret"].default
_JWT_SECRET_MIN_LEN = 32


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage application lifespan — startup and shutdown."""
    # Hatchet: initialise client if token is configured.
    # Non-fatal: if no token is set the app runs without workflow dispatch.
    import logging

    _log = logging.getLogger(__name__)
    _hatchet_ok = False
    try:
        from zeiterfassung.core.hatchet import get_hatchet

        get_hatchet()
        _hatchet_ok = True
    except RuntimeError as exc:
        _log.warning("Hatchet not configured — workflows disabled. (%s)", exc)
    except Exception as exc:  # noqa: BLE001
        _log.warning("Hatchet init failed — workflows disabled. (%s)", exc)

    # Krankmeldungs-Postfach (M365/Graph): Poll-Loop nur, wenn konfiguriert.
    import asyncio

    from zeiterfassung.config import get_settings

    _settings = get_settings()

    # Loop laeuft immer; ob wirklich abgerufen wird, entscheidet die in den
    # Einstellungen gepflegte Konfiguration (bzw. Env-Fallback) pro Durchlauf.
    async def _sickmail_loop() -> None:
        from zeiterfassung.api.deps import _AsyncSessionLocal
        from zeiterfassung.services import sickmail_service

        while True:
            try:
                async with _AsyncSessionLocal() as db:
                    imported = await sickmail_service.fetch_mailbox(db)
                    if imported:
                        _log.info("Krankmeldungs-Postfach: %d neue Mail(s)", imported)
            except Exception as exc:  # noqa: BLE001 — Loop darf nie sterben
                _log.warning("Krankmeldungs-Postfach: Abruf fehlgeschlagen (%s)", exc)
            await asyncio.sleep(_settings.sickmail_poll_seconds)

    _sickmail_task = asyncio.create_task(_sickmail_loop())

    # Austritts-Automatik: einmal beim Start und dann taeglich alle faelligen
    # Austritte archivieren (Austrittsdatum ueberschritten -> Mitarbeiter ins
    # Archiv, NFC-Karte freigeben, Login-Accounts deaktivieren).
    async def _archive_loop() -> None:
        from zeiterfassung.api.deps import _AsyncSessionLocal
        from zeiterfassung.services import user_service

        while True:
            try:
                async with _AsyncSessionLocal() as db:
                    n = await user_service.archive_due_terminations(db)
                    if n:
                        _log.info("Austritts-Archivierung: %d Mitarbeiter archiviert", n)
            except Exception as exc:  # noqa: BLE001 — Loop darf nie sterben
                _log.warning("Austritts-Archivierung fehlgeschlagen (%s)", exc)
            await asyncio.sleep(24 * 3600)

    _archive_task = asyncio.create_task(_archive_loop())

    # Ueberstunden-Monatsabschluss: taeglich alle offenen Vormonate abschliessen
    # (idempotent; Kappe/Auszahlung je overtime_payout_mode, Spec 2026-07-09).
    # Nur Soll-Tenants (export_basis="soll") werden automatisch abgeschlossen,
    # siehe overtime_service.settle_previous_month_all.
    async def _settle_loop() -> None:
        from zeiterfassung.api.deps import _AsyncSessionLocal
        from zeiterfassung.services import overtime_service

        while True:
            try:
                async with _AsyncSessionLocal() as db:
                    n = await overtime_service.settle_previous_month_all(db)
                    if n:
                        _log.info("Ueberstunden-Abschluss: %d Mitarbeiter-Monate", n)
            except Exception as exc:  # noqa: BLE001 — Loop darf nie sterben
                _log.warning("Ueberstunden-Abschluss fehlgeschlagen (%s)", exc)
            await asyncio.sleep(24 * 3600)

    _settle_task = asyncio.create_task(_settle_loop())

    # Automatisches Ausstempeln (Spec 2026-08-03): offene Schichten, die laenger
    # als settings.auto_clockout_hours (Default 14 h) laufen, werden als
    # "Ausstempeln vergessen" gekappt (0 h + Flag, bis HR die echte Zeit
    # eintraegt). Sweep alle settings.auto_clockout_sweep_minutes.
    async def _auto_clockout_loop() -> None:
        from zeiterfassung.api.deps import _AsyncSessionLocal
        from zeiterfassung.services import clock_service

        while True:
            await asyncio.sleep(_settings.auto_clockout_sweep_minutes * 60)
            try:
                async with _AsyncSessionLocal() as db:
                    n = await clock_service.auto_clockout_overdue_shifts(db)
                    if n:
                        await db.commit()
                        _log.info(
                            "Auto-Ausstempeln: %d Schicht(en) nach %d h gekappt",
                            n,
                            _settings.auto_clockout_hours,
                        )
            except Exception as exc:  # noqa: BLE001 — Loop darf nie sterben
                _log.warning("Auto-Ausstempeln fehlgeschlagen (%s)", exc)

    _auto_clockout_task = asyncio.create_task(_auto_clockout_loop())

    # Monatlicher Stundenzettel-Versand (Spec 2026-08-07): ab dem 1. wird der
    # Vormonat an alle markierten Mitarbeiter gemailt. Der Service ist
    # idempotent (Versandprotokoll), daher ist die Frequenz unkritisch.
    async def _timesheet_mail_loop() -> None:
        from zeiterfassung.api.deps import _AsyncSessionLocal
        from zeiterfassung.services import timesheet_mail_service

        while True:
            try:
                async with _AsyncSessionLocal() as db:
                    c = await timesheet_mail_service.send_all_tenants(db)
                    if c["sent"] or c["errors"]:
                        _log.info("Stundenzettel-Versand: %s", c)
            except Exception as exc:  # noqa: BLE001 — Loop darf nie sterben
                _log.warning("Stundenzettel-Versand fehlgeschlagen (%s)", exc)
            await asyncio.sleep(6 * 3600)

    _timesheet_mail_task = asyncio.create_task(_timesheet_mail_loop())

    # Auto-Erfassung: Tagesboegen regelmaessig nachziehen. Ohne diesen Job
    # entstehen sie erst beim Oeffnen der Detailseite — der Teamkalender zeigte
    # solche Tage bis dahin faelschlich als leer.
    async def _auto_timesheet_loop() -> None:
        from zeiterfassung.api.deps import _AsyncSessionLocal
        from zeiterfassung.services import clock_service

        while True:
            try:
                async with _AsyncSessionLocal() as db:
                    n = await clock_service.backfill_auto_timesheets(db)
                    await db.commit()
                    if n:
                        _log.info("Auto-Tagesboegen geprueft: %d Mitarbeiter", n)
            except Exception as exc:  # noqa: BLE001 — Loop darf nie sterben
                _log.warning("Auto-Tagesboegen fehlgeschlagen (%s)", exc)
            await asyncio.sleep(3600)

    _auto_ts_task = asyncio.create_task(_auto_timesheet_loop())

    yield

    if _sickmail_task is not None:
        _sickmail_task.cancel()
    if _archive_task is not None:
        _archive_task.cancel()
    if _settle_task is not None:
        _settle_task.cancel()
    if _auto_clockout_task is not None:
        _auto_clockout_task.cancel()
    if _auto_ts_task is not None:
        _auto_ts_task.cancel()
    if _timesheet_mail_task is not None:
        _timesheet_mail_task.cancel()

    # Hatchet shutdown
    if _hatchet_ok:
        try:
            from zeiterfassung.core.hatchet import close_hatchet

            close_hatchet()
        except Exception as exc:  # noqa: BLE001
            _log.warning("Hatchet shutdown error (ignored): %s", exc)


def _check_jwt_secret(settings) -> None:  # noqa: ANN001
    """Abbruch (Prod) bzw. Warnung (Debug), wenn das JWT-Secret unsicher ist.

    Unsicher heisst: der ausgelieferte CHANGE_ME-Default, oder kuerzer als
    32 Zeichen. Ein schwaches/Default-Secret macht jeden Token faelschbar —
    das darf ausserhalb von Debug/Dev niemals unbemerkt live gehen (fkw.4).
    """
    import logging

    weak = (
        settings.jwt_secret == _JWT_SECRET_DEFAULT or len(settings.jwt_secret) < _JWT_SECRET_MIN_LEN
    )
    if not weak:
        return
    msg = (
        "ZEITERFASSUNG_JWT_SECRET ist nicht gesetzt (Default) oder kuerzer als "
        f"{_JWT_SECRET_MIN_LEN} Zeichen. In Produktion MUSS ein langes, "
        "zufaelliges Secret gesetzt werden, z. B. `openssl rand -hex 32`."
    )
    if settings.debug:
        logging.getLogger(__name__).warning(msg)
    else:
        raise RuntimeError(msg)


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    settings = get_settings()
    _check_jwt_secret(settings)

    app = FastAPI(
        title="Zeiterfassung API",
        description="Multi-tenant time-tracking and payroll-export API",
        version="0.1.0",
        docs_url="/docs" if settings.debug else None,
        redoc_url="/redoc" if settings.debug else None,
        lifespan=lifespan,
    )

    # CORS — allow_credentials + allow_origins=["*"] doesn't work in browsers.
    # In dev mode, allow all origins without credentials.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Security-Header auf jeder Antwort (login-hardening, Spec Abschnitt 3).
    app.add_middleware(SecurityHeadersMiddleware)

    # JSON API only — the frontend (Nuxt/Jinja) lives in its own service and
    # talks to this backend purely over HTTP. No server-rendered UI here.
    app.include_router(api_router, prefix="/api/v1")

    @app.get("/health", tags=["health"])
    async def health_check() -> dict[str, str]:
        return {"status": "ok", "version": "0.1.0"}

    return app


app = create_app()
