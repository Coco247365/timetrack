"""Tenant-level admin settings endpoints: feature toggles, sick-mail inbox
config, work schedules, ICS calendar feed token.

Split out of the former monolithic ``api/v1/admin.py`` (zeiterfassung-6pcv).
Mounted under ``/admin`` (prefix applied once by ``api/v1/router.py`` via the
``admin`` package aggregator — see ``api/v1/admin/__init__.py``).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from zeiterfassung.api.deps import AuthUser, DBSession
from zeiterfassung.services import admin_settings_service

router = APIRouter()


def _can_manage_features(u: AuthUser) -> bool:
    """Funktions-Schalter und Kalender-Abo-Token: eigenes feines Recht.

    ``admin:settings`` reicht bewusst NICHT — z. B. Buchhaltung pflegt Lohnarten,
    soll aber keine Firmen-Funktionen an-/abschalten (Rechte-Vergabe pro Rolle
    unter "Rollen & Rechte").
    """
    return u.is_superadmin or u.has_permission("admin:features")


def _require_settings_perm(admin: AuthUser) -> None:
    if not admin.has_permission("admin:settings"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="admin:settings erforderlich"
        )


@router.get("/features")
async def get_features(db: DBSession, admin: AuthUser) -> dict:
    """Aktive Funktions-Schalter des Mandanten.

    Fuer JEDEN eingeloggten Nutzer lesbar (die Navigation braucht sie),
    geaendert wird nur ueber PATCH (admin:settings).
    """
    return await admin_settings_service.get_features(db, admin.tenant_id)


@router.patch("/features")
async def update_features(body: dict, db: DBSession, admin: AuthUser) -> dict:
    """Funktions-Schalter aendern (nur bekannte Schluessel, admin:features)."""
    if not _can_manage_features(admin):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="admin:features erforderlich"
        )
    return await admin_settings_service.update_features(db, admin.tenant_id, body)


@router.get("/vacation-settings")
async def get_vacation_settings(db: DBSession, admin: AuthUser) -> dict:
    """Mandantenweiter Default-Urlaubsanspruch fuer neue Mitarbeiter."""
    _require_settings_perm(admin)
    return await admin_settings_service.get_vacation_settings(db, admin.tenant_id)


@router.patch("/vacation-settings")
async def update_vacation_settings(body: dict, db: DBSession, admin: AuthUser) -> dict:
    """Mandanten-Default fuer den Urlaubsanspruch setzen (admin:settings)."""
    _require_settings_perm(admin)
    return await admin_settings_service.update_vacation_settings(db, admin.tenant_id, body)


@router.get("/sickmail-config")
async def get_sickmail_config(db: DBSession, admin: AuthUser) -> dict:
    """Alle Postfaecher (Geheimnisse maskiert)."""
    _require_settings_perm(admin)
    return await admin_settings_service.get_sickmail_config(db, admin.tenant_id)


@router.patch("/sickmail-config")
async def upsert_sickmail_box(body: dict, db: DBSession, admin: AuthUser) -> dict:
    """Postfach anlegen oder aendern (body.id = aendern, sonst neu).

    Leere Geheimnis-Felder behalten den gespeicherten Wert.
    """
    _require_settings_perm(admin)
    return await admin_settings_service.upsert_sickmail_box(db, admin.tenant_id, body)


@router.delete("/sickmail-config/{box_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_sickmail_box(box_id: str, db: DBSession, admin: AuthUser) -> None:
    """Postfach entfernen."""
    _require_settings_perm(admin)
    await admin_settings_service.delete_sickmail_box(db, admin.tenant_id, box_id)


@router.post("/sickmail-config/{box_id}/test")
async def test_sickmail_box(box_id: str, db: DBSession, admin: AuthUser) -> dict:
    """Ein bestimmtes Postfach testen (Login + Ordner oeffnen)."""
    _require_settings_perm(admin)
    return await admin_settings_service.test_sickmail_box(db, admin.tenant_id, box_id)


@router.get("/work-schedules", response_model=list[dict])
async def get_work_schedules(db: DBSession, admin: AuthUser) -> list[dict]:
    """List the tenant's work schedules (Arbeitszeitmodelle).

    Readable by Admin/GF/HR because the employee-edit form needs the list.
    Stored globally in ``Tenant.settings["work_schedules"]``.
    """
    if not (
        admin.has_permission("users:read_all")
        or admin.has_permission("users:write")
        or admin.has_permission("admin")
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions"
        )
    return await admin_settings_service.get_work_schedules(db, admin.tenant_id)


@router.post("/work-schedules")
async def upsert_work_schedule(body: dict, db: DBSession, admin: AuthUser) -> dict:
    """Create or update a work schedule by ``id`` (upsert).

    Requires ``admin:settings``. If ``id`` is missing/empty a new one is generated.
    Stored in ``Tenant.settings["work_schedules"]``.
    """
    _require_settings_perm(admin)
    return await admin_settings_service.upsert_work_schedule(db, admin.tenant_id, body)


@router.post("/work-schedules/remove")
async def remove_work_schedule(body: dict, db: DBSession, admin: AuthUser) -> dict:
    """Remove a work schedule by ``id`` from ``Tenant.settings["work_schedules"]``.

    Requires ``admin:settings``.
    """
    _require_settings_perm(admin)
    return await admin_settings_service.remove_work_schedule(db, admin.tenant_id, body)


@router.get("/ics-feed")
async def get_ics_feed(db: DBSession, admin: AuthUser) -> dict:
    """Abo-URL fuer den Abwesenheits-Kalender (Token wird bei Bedarf erzeugt)."""
    if not admin.has_permission("admin:settings") and not _can_manage_features(admin):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="admin:settings erforderlich"
        )
    return await admin_settings_service.get_ics_feed(db, admin.tenant_id)


@router.post("/ics-feed/rotate")
async def rotate_ics_feed(db: DBSession, admin: AuthUser) -> dict:
    """Neuen Token erzeugen — alte Abo-URLs werden damit ungueltig."""
    if not _can_manage_features(admin):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="admin:features erforderlich"
        )
    return await admin_settings_service.rotate_ics_feed(db, admin.tenant_id)


# --- Kiosk-Kurzmeldungen (#189) -------------------------------------
# Verwaltung ueber die Weboberflaeche; Anzeige am Terminal ueber die
# device-authentifizierten Endpunkte in api/v1/clock.py.


@router.get("/kiosk-messages")
async def list_kiosk_messages(
    db: DBSession,
    admin: AuthUser,
    employee_id: str | None = None,
    broadcasts_only: bool = False,
) -> list[dict]:
    """Meldungen auflisten (Rundmeldungen und/oder je Mitarbeiter)."""
    import uuid as _uuid

    from zeiterfassung.services import kiosk_message_service

    _require_settings_perm(admin)
    emp = _uuid.UUID(employee_id) if employee_id else None
    return await kiosk_message_service.list_messages(
        db, admin.tenant_id, employee_id=emp, broadcasts_only=broadcasts_only
    )


@router.post("/kiosk-messages", status_code=status.HTTP_201_CREATED)
async def create_kiosk_message(body: dict, db: DBSession, admin: AuthUser) -> dict:
    """Meldung anlegen. body: {text, employee_id?, active_until?}."""
    import uuid as _uuid
    from datetime import date as _date

    from zeiterfassung.services import kiosk_message_service

    _require_settings_perm(admin)
    emp = _uuid.UUID(body["employee_id"]) if body.get("employee_id") else None
    von = _date.fromisoformat(body["active_from"]) if body.get("active_from") else None
    until = _date.fromisoformat(body["active_until"]) if body.get("active_until") else None
    return await kiosk_message_service.create_message(
        db,
        admin.tenant_id,
        text=body.get("text", ""),
        employee_id=emp,
        active_from=von,
        active_until=until,
        created_by_account_id=admin.account_id,
    )


@router.delete("/kiosk-messages/{message_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_kiosk_message(message_id: str, db: DBSession, admin: AuthUser) -> None:
    """Meldung entfernen."""
    import uuid as _uuid

    from zeiterfassung.services import kiosk_message_service

    _require_settings_perm(admin)
    await kiosk_message_service.delete_message(db, admin.tenant_id, _uuid.UUID(message_id))


# ── Stundenzettel-Versand (Spec 2026-08-07) ─────────────────────────────────


@router.get("/timesheet-mail")
async def get_timesheet_mail(db: DBSession, admin: AuthUser) -> dict:
    """Konfiguration + Empfängerliste + letzte Log-Einträge."""
    from sqlalchemy import desc, select

    from zeiterfassung.db.scoping import tenant_scoped
    from zeiterfassung.models.tenant import Tenant
    from zeiterfassung.models.timesheet_mail_log import TimesheetMailLog
    from zeiterfassung.models.user import User
    from zeiterfassung.services import timesheet_mail_service

    _require_settings_perm(admin)
    tenant = await db.get(Tenant, admin.tenant_id)
    cfg = timesheet_mail_service.get_config(tenant)

    emp_stmt = (
        tenant_scoped(select(User), User, admin.tenant_id)
        .where(User.is_active == True)  # noqa: E712
        .order_by(User.last_name, User.first_name)
    )
    employees = (await db.execute(emp_stmt)).scalars().all()
    recipients = [
        {
            "employee_id": str(u.id),
            "name": f"{u.first_name} {u.last_name}",
            "email": u.email,
            "enabled": bool(u.monthly_timesheet_email),
        }
        for u in employees
    ]

    log_stmt = (
        tenant_scoped(select(TimesheetMailLog), TimesheetMailLog, admin.tenant_id)
        .order_by(desc(TimesheetMailLog.created_at))
        .limit(50)
    )
    logs = (await db.execute(log_stmt)).scalars().all()
    names = {str(u.id): f"{u.first_name} {u.last_name}" for u in employees}
    log_rows = [
        {
            "employee_name": names.get(str(entry.employee_id), "—"),
            "year_month": entry.year_month.isoformat(),
            "sent_to": entry.sent_to,
            "status": entry.status,
            "detail": entry.detail,
            "created_at": entry.created_at.isoformat() if entry.created_at else None,
        }
        for entry in logs
    ]
    smtp = dict(cfg["smtp"])
    # Passwort nie zurueckliefern, nur ob eins gesetzt ist.
    smtp["password_set"] = bool(smtp.pop("password", ""))
    # Auswaehlbare M365-Postfaecher fuer den Versand (id + Adresse).
    from zeiterfassung.services.sickmail_service import resolve_mail_configs

    mailboxes = [
        {"id": str(c.get("id")), "mailbox": str(c.get("mailbox") or "")}
        for c in resolve_mail_configs(tenant)
        if c.get("provider") == "m365"
    ]
    return {
        "enabled": cfg["enabled"],
        "sender": cfg["sender"],
        "body_text": cfg["body_text"],
        "send_day": cfg["send_day"],
        "transport": cfg["transport"],
        "smtp": smtp,
        "mailbox_id": cfg["mailbox_id"],
        "mailboxes": mailboxes,
        "problem": timesheet_mail_service.transport_ready(tenant),
        "recipients": recipients,
        "log": log_rows,
    }


@router.post("/timesheet-mail")
async def update_timesheet_mail(body: dict, db: DBSession, admin: AuthUser) -> dict:
    """Hauptschalter/Absender speichern. body: {enabled?, sender?}."""
    from sqlalchemy.orm.attributes import flag_modified

    from zeiterfassung.models.tenant import Tenant
    from zeiterfassung.services import timesheet_mail_service

    _require_settings_perm(admin)
    tenant = await db.get(Tenant, admin.tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    settings = dict(tenant.settings or {})
    cfg = dict(settings.get("timesheet_mail") or {})
    if "enabled" in body:
        cfg["enabled"] = bool(body["enabled"])
    if "sender" in body:
        cfg["sender"] = str(body["sender"] or "").strip()
    if "body_text" in body:
        cfg["body_text"] = str(body["body_text"] or "").strip()[:1000]
    if "send_day" in body:
        try:
            cfg["send_day"] = min(28, max(1, int(body["send_day"])))
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Versandtag muss eine Zahl zwischen 1 und 28 sein.",
            ) from None
    if "transport" in body:
        t = str(body["transport"] or "m365").lower()
        if t not in ("m365", "smtp"):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="transport muss 'm365' oder 'smtp' sein.",
            )
        cfg["transport"] = t
    if "mailbox_id" in body:
        mid = body["mailbox_id"]
        cfg["mailbox_id"] = str(mid) if mid else None
    if "smtp" in body and isinstance(body["smtp"], dict):
        cur = dict(cfg.get("smtp") or {})
        inc = body["smtp"]
        for key in ("host", "user"):
            if key in inc:
                cur[key] = str(inc[key] or "").strip()
        if "port" in inc:
            try:
                cur["port"] = int(inc["port"])
            except (TypeError, ValueError):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="SMTP-Port muss eine Zahl sein.",
                ) from None
        if "starttls" in inc:
            cur["starttls"] = bool(inc["starttls"])
        # Leeres Passwort-Feld laesst das gespeicherte unveraendert.
        if inc.get("password"):
            cur["password"] = str(inc["password"])
        cfg["smtp"] = cur
    settings["timesheet_mail"] = cfg
    tenant.settings = settings
    flag_modified(tenant, "settings")
    await db.commit()
    # Passwort nie zurueckliefern (wie im GET): nur ob eins gesetzt ist —
    # sonst laege es im Klartext in jeder Speichern-Antwort und in Proxylogs.
    out = timesheet_mail_service.get_config(tenant)
    smtp_out = dict(out.get("smtp") or {})
    smtp_out["password_set"] = bool(smtp_out.pop("password", ""))
    out["smtp"] = smtp_out
    out["problem"] = timesheet_mail_service.transport_ready(tenant)
    return out


@router.post("/timesheet-mail/recipients")
async def set_timesheet_mail_recipient(body: dict, db: DBSession, admin: AuthUser) -> dict:
    """Häkchen für einen Mitarbeiter setzen. body: {employee_id, enabled}."""
    import uuid as _uuid

    from sqlalchemy import select

    from zeiterfassung.db.scoping import tenant_scoped
    from zeiterfassung.models.user import User

    _require_settings_perm(admin)
    emp_id = _uuid.UUID(str(body.get("employee_id")))
    stmt = tenant_scoped(select(User), User, admin.tenant_id).where(User.id == emp_id)
    user = (await db.execute(stmt)).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")
    user.monthly_timesheet_email = bool(body.get("enabled"))
    await db.commit()
    return {"employee_id": str(user.id), "enabled": user.monthly_timesheet_email}


@router.post("/timesheet-mail/send-now")
async def timesheet_mail_send_now(db: DBSession, admin: AuthUser) -> dict:
    """Manueller Anstoß: Vormonat für alle markierten Mitarbeiter senden."""
    from zeiterfassung.models.tenant import Tenant
    from zeiterfassung.services import timesheet_mail_service

    _require_settings_perm(admin)
    tenant = await db.get(Tenant, admin.tenant_id)
    cfg = timesheet_mail_service.get_config(tenant)
    if not cfg["enabled"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Versand ist deaktiviert — erst den Schalter aktivieren.",
        )
    problem = timesheet_mail_service.transport_ready(tenant)
    if problem is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=problem)
    # Manueller Anstoss ignoriert den eingestellten Versandtag.
    return await timesheet_mail_service.send_previous_month(db, admin.tenant_id, force_day=True)
