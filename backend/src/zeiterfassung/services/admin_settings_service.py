"""Tenant-level admin settings: feature toggles, sick-mail inbox config,
work schedules (Arbeitszeitmodelle), ICS calendar feed token.

Moved verbatim out of the former monolithic ``api/v1/admin.py``
(zeiterfassung-6pcv). The sickmail "Verbindung fehlgeschlagen" error was
additionally converted from a raw ``detail=f"...{exc}"`` to the typed
``api_error()``/``ErrorCode`` pattern (bead #132 follow-up, noted in the
6pcv bead description).
"""

from __future__ import annotations

import secrets
import uuid

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from zeiterfassung.core.errors import ErrorCode, api_error
from zeiterfassung.services.tenant_service import get_tenant_or_404

# ---------------------------------------------------------------------------
# Feature toggles (Funktions-Schalter)
# ---------------------------------------------------------------------------

_FEATURE_DEFAULTS = {
    "org_chart": True,
    "sick_mail": True,
    "sick_mail_auto_approve": False,
    "payroll_bookings": False,
    # Teamkalender: vergangene Tage mit erfasster Arbeitszeit gruen markieren
    # (heute = Live-Anwesenheit, davon unabhaengig). #187.
    "team_past_worked": True,
    # ...aber nur an Tagen mit ECHTEN Stempelungen (computed_from_events),
    # sonst waeren Auto-Erfassungs-MA jeden Werktag gruen (#191).
    "team_past_worked_stamped_only": False,
}


async def get_features(db: AsyncSession, tenant_id: uuid.UUID) -> dict:
    """Aktive Funktions-Schalter des Mandanten."""
    tenant = await get_tenant_or_404(db, tenant_id)
    stored = (tenant.settings or {}).get("features") or {}
    return {**_FEATURE_DEFAULTS, **stored}


async def update_features(db: AsyncSession, tenant_id: uuid.UUID, body: dict) -> dict:
    """Funktions-Schalter aendern (nur bekannte Schluessel)."""
    tenant = await get_tenant_or_404(db, tenant_id)
    settings = dict(tenant.settings or {})
    features = dict(settings.get("features") or {})
    for key, value in body.items():
        if key in _FEATURE_DEFAULTS:
            features[key] = bool(value)
    settings["features"] = features
    tenant.settings = settings
    await db.commit()
    return {**_FEATURE_DEFAULTS, **features}


# ---------------------------------------------------------------------------
# Urlaubsanspruch-Default (7tu: war fix 30 im Schema vs. fix 24 im Service-
# Fallback -- jetzt EIN mandantenweiter Default fuer beide Stellen).
# ---------------------------------------------------------------------------


async def get_vacation_settings(db: AsyncSession, tenant_id: uuid.UUID) -> dict:
    """Mandantenweiter Default-Urlaubsanspruch (Tage/Jahr) fuer neue Mitarbeiter."""
    from zeiterfassung.services.absence_service import (
        DEFAULT_VACATION_DAYS_PER_YEAR,
        resolve_default_vacation_entitlement_days,
    )

    tenant = await get_tenant_or_404(db, tenant_id)
    return {
        "default_vacation_entitlement_days": resolve_default_vacation_entitlement_days(tenant),
        "is_custom": (tenant.settings or {}).get("default_vacation_entitlement_days") is not None,
        "fallback_default": DEFAULT_VACATION_DAYS_PER_YEAR,
    }


async def update_vacation_settings(db: AsyncSession, tenant_id: uuid.UUID, body: dict) -> dict:
    """Mandanten-Default fuer den Urlaubsanspruch setzen (0..365 Tage)."""
    tenant = await get_tenant_or_404(db, tenant_id)
    value = body.get("default_vacation_entitlement_days")
    if value is not None:
        value = float(value)
        if not (0 <= value <= 365):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="default_vacation_entitlement_days muss zwischen 0 und 365 liegen",
            )
    settings = dict(tenant.settings or {})
    if value is None:
        settings.pop("default_vacation_entitlement_days", None)
    else:
        settings["default_vacation_entitlement_days"] = value
    tenant.settings = settings
    await db.commit()
    return await get_vacation_settings(db, tenant_id)


# ---------------------------------------------------------------------------
# Sick-mail inboxes (Krankmeldungs-Postfaecher)
# ---------------------------------------------------------------------------

_SICKMAIL_SECRETS = ("client_secret", "password")
_SICKMAIL_FIELDS = {
    "m365": ("tenant_id", "client_id", "client_secret", "mailbox"),
    "imap": ("host", "port", "user", "password", "folder"),
}


def _mask_box(box: dict) -> dict:
    out = {k: v for k, v in box.items() if k not in _SICKMAIL_SECRETS}
    out["has_client_secret"] = bool(box.get("client_secret"))
    out["has_password"] = bool(box.get("password"))
    from zeiterfassung.services.sickmail_service import _config_complete

    out["complete"] = _config_complete(box)
    return out


async def get_sickmail_config(db: AsyncSession, tenant_id: uuid.UUID) -> dict:
    """Alle Postfaecher (Geheimnisse maskiert)."""
    from zeiterfassung.services.sickmail_service import get_mailbox_configs, resolve_mail_configs

    tenant = await get_tenant_or_404(db, tenant_id)
    boxes = get_mailbox_configs(tenant)
    active = resolve_mail_configs(tenant)
    return {
        "boxes": [_mask_box(b) for b in boxes],
        "active_count": len(active),
        "env_fallback": not boxes and bool(active),
    }


async def upsert_sickmail_box(db: AsyncSession, tenant_id: uuid.UUID, body: dict) -> dict:
    """Postfach anlegen oder aendern (body.id = aendern, sonst neu).

    Leere Geheimnis-Felder behalten den gespeicherten Wert.
    """
    from zeiterfassung.services.sickmail_service import get_mailbox_configs

    provider = body.get("provider")
    if provider not in _SICKMAIL_FIELDS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="provider muss 'm365' oder 'imap' sein",
        )
    tenant = await get_tenant_or_404(db, tenant_id)
    boxes = get_mailbox_configs(tenant)

    box_id = body.get("id") or str(uuid.uuid4())[:8]
    existing = next((b for b in boxes if b.get("id") == box_id), None)
    # "intake": ob der Krankmeldungs-Abruf dieses Postfach lesen darf. Default
    # True (bewahrt Bestandsverhalten); ein reines Versand-Postfach setzt False.
    if "intake" in body:
        intake = bool(body["intake"])
    elif existing is not None:
        intake = existing.get("intake", True) is not False
    else:
        intake = True
    new_box = {"id": box_id, "provider": provider, "intake": intake}
    for key in _SICKMAIL_FIELDS[provider]:
        value = body.get(key)
        if key in _SICKMAIL_SECRETS:
            new_box[key] = value if value else (existing or {}).get(key)
        elif value is not None:
            new_box[key] = value
        elif existing and existing.get("provider") == provider:
            new_box[key] = existing.get(key)

    boxes = [b for b in boxes if b.get("id") != box_id] + [new_box]
    settings = dict(tenant.settings or {})
    settings["sickmail_boxes"] = boxes
    settings.pop("sickmail", None)  # Alt-Format ist damit ueberfuehrt
    tenant.settings = settings
    await db.commit()
    return {"ok": True, "id": box_id, "box": _mask_box(new_box)}


async def delete_sickmail_box(db: AsyncSession, tenant_id: uuid.UUID, box_id: str) -> None:
    """Postfach entfernen."""
    from zeiterfassung.services.sickmail_service import get_mailbox_configs

    tenant = await get_tenant_or_404(db, tenant_id)
    boxes = [b for b in get_mailbox_configs(tenant) if b.get("id") != box_id]
    settings = dict(tenant.settings or {})
    settings["sickmail_boxes"] = boxes
    settings.pop("sickmail", None)
    tenant.settings = settings
    await db.commit()


async def test_sickmail_box(db: AsyncSession, tenant_id: uuid.UUID, box_id: str) -> dict:
    """Ein bestimmtes Postfach testen (Login + Ordner oeffnen)."""
    from zeiterfassung.services.sickmail_service import (
        _config_complete,
        get_mailbox_configs,
        resolve_mail_configs,
        test_mail_config,
    )

    tenant = await get_tenant_or_404(db, tenant_id)
    box = next((b for b in get_mailbox_configs(tenant) if b.get("id") == box_id), None)
    if box is None and box_id == "env":
        box = next((b for b in resolve_mail_configs(tenant) if b.get("id") == "env"), None)
    if box is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Postfach nicht gefunden")
    if not _config_complete(box):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Konfiguration unvollstaendig (fehlt das Passwort?)",
        )
    try:
        detail = await test_mail_config(box)
    except Exception as exc:  # noqa: BLE001
        # Typed api_error() instead of a raw f"...{exc}" detail (bead #132
        # follow-up folded into 6pcv) — the underlying imaplib/graph error
        # text stays out of the public API contract; ``code`` is stable.
        raise api_error(
            status.HTTP_502_BAD_GATEWAY, exc, code=ErrorCode.SICK_MAIL_FETCH_FAILED
        ) from exc
    return {"ok": True, "detail": detail}


# ---------------------------------------------------------------------------
# Work schedules (Arbeitszeitmodelle)
# ---------------------------------------------------------------------------


async def get_work_schedules(db: AsyncSession, tenant_id: uuid.UUID) -> list[dict]:
    """List the tenant's work schedules. Stored in ``Tenant.settings["work_schedules"]``."""
    tenant = await get_tenant_or_404(db, tenant_id)
    return list((tenant.settings or {}).get("work_schedules") or [])


async def upsert_work_schedule(db: AsyncSession, tenant_id: uuid.UUID, body: dict) -> dict:
    """Create or update a work schedule by ``id`` (upsert).

    If ``id`` is missing/empty a new one is generated.
    """
    tenant = await get_tenant_or_404(db, tenant_id)

    body = body or {}
    ws_id = str(body.get("id") or "").strip()
    if not ws_id:
        ws_id = uuid.uuid4().hex

    schedule = {
        "id": ws_id,
        "name": str(body.get("name") or "").strip(),
        "weekly_hours": float(body.get("weekly_hours", 0) or 0),
        "daily_hours": float(body.get("daily_hours", 0) or 0),
        "break_minutes": int(body.get("break_minutes", 30) or 0),
        "days": str(body.get("days") or "").strip(),
        "start": str(body.get("start") or "").strip(),
        "end": str(body.get("end") or "").strip(),
        "active": bool(body.get("active", True)),
    }

    existing = list((tenant.settings or {}).get("work_schedules") or [])
    replaced = False
    new_list = []
    for s in existing:
        if str(s.get("id")) == ws_id:
            new_list.append(schedule)
            replaced = True
        else:
            new_list.append(s)
    if not replaced:
        new_list.append(schedule)

    settings = dict(tenant.settings or {})
    settings["work_schedules"] = new_list
    tenant.settings = settings
    await db.commit()
    return {"ok": True, "id": ws_id}


async def remove_work_schedule(db: AsyncSession, tenant_id: uuid.UUID, body: dict) -> dict:
    """Remove a work schedule by ``id`` from ``Tenant.settings["work_schedules"]``."""
    tenant = await get_tenant_or_404(db, tenant_id)

    ws_id = str((body or {}).get("id") or "").strip()
    existing = list((tenant.settings or {}).get("work_schedules") or [])
    new_list = [s for s in existing if str(s.get("id")) != ws_id]

    settings = dict(tenant.settings or {})
    settings["work_schedules"] = new_list
    tenant.settings = settings
    await db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# ICS calendar feed token
# ---------------------------------------------------------------------------


async def get_ics_feed(db: AsyncSession, tenant_id: uuid.UUID) -> dict:
    """Abo-URL fuer den Abwesenheits-Kalender (Token wird bei Bedarf erzeugt)."""
    tenant = await get_tenant_or_404(db, tenant_id)
    settings = dict(tenant.settings or {})
    token = settings.get("ics_feed_token")
    if not token:
        token = secrets.token_urlsafe(24)
        settings["ics_feed_token"] = token
        tenant.settings = settings
        await db.commit()
    return {"path": f"/api/v1/absences/feed.ics?token={token}"}


async def rotate_ics_feed(db: AsyncSession, tenant_id: uuid.UUID) -> dict:
    """Neuen Token erzeugen — alte Abo-URLs werden damit ungueltig."""
    tenant = await get_tenant_or_404(db, tenant_id)
    settings = dict(tenant.settings or {})
    settings["ics_feed_token"] = secrets.token_urlsafe(24)
    tenant.settings = settings
    await db.commit()
    return {"path": f"/api/v1/absences/feed.ics?token={settings['ics_feed_token']}"}
