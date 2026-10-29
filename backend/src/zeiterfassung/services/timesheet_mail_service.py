"""Monatlicher Stundenzettel-Versand per E-Mail (Spec 2026-08-07).

Verschickt fuer ausgewaehlte Mitarbeiter (users.monthly_timesheet_email)
den Stundenzettel des Vormonats als PDF an die Personalakten-E-Mail.
Versandweg ist Microsoft Graph ``sendMail`` mit den vorhandenen
M365-Zugangsdaten aus der Krankmeldungs-Konfiguration (die App braucht
zusaetzlich die Berechtigung **Mail.Send**).

Idempotenz: pro (Mitarbeiter, Monat) gilt der Versand als erledigt, sobald
ein ``sent``-Eintrag im :class:`TimesheetMailLog` steht. ``error`` und
``skipped_no_email`` blockieren nicht — der naechste Lauf probiert es
wieder (z. B. nachdem HR eine E-Mail nachgetragen hat).
"""

from __future__ import annotations

import base64
import logging
import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from zeiterfassung.db.scoping import tenant_scoped
from zeiterfassung.models.tenant import Tenant
from zeiterfassung.models.timesheet_mail_log import TimesheetMailLog
from zeiterfassung.models.user import User
from zeiterfassung.services import report_service
from zeiterfassung.services.sickmail_service import _graph_token, resolve_mail_configs
from zeiterfassung.services.timesheet_pdf import build_timesheet_pdf

_log = logging.getLogger(__name__)

_MONTHS = [
    "Januar",
    "Februar",
    "März",
    "April",
    "Mai",
    "Juni",
    "Juli",
    "August",
    "September",
    "Oktober",
    "November",
    "Dezember",
]


def get_config(tenant: Tenant | None) -> dict:
    """Versand-Konfiguration des Mandanten.

    ``send_day``: Tag im Monat, ab dem der Vormonat verschickt wird (1..28 —
    darueber gaebe es Monate ohne diesen Tag). ``transport``: "m365" nutzt die
    Microsoft-Zugangsdaten, "smtp" einen eigenen Mailserver.
    """
    settings = (tenant.settings or {}) if tenant else {}
    cfg = settings.get("timesheet_mail") or {}
    try:
        send_day = int(cfg.get("send_day") or 1)
    except (TypeError, ValueError):
        send_day = 1
    transport = str(cfg.get("transport") or "m365").lower()
    if transport not in ("m365", "smtp"):
        transport = "m365"
    smtp = cfg.get("smtp") or {}
    return {
        "enabled": bool(cfg.get("enabled")),
        "sender": str(cfg.get("sender") or ""),
        # Welches Postfach der M365-Versand nutzt (None = erstes vollstaendige).
        "mailbox_id": (str(cfg.get("mailbox_id")) if cfg.get("mailbox_id") else None),
        # Frei konfigurierbarer Standardtext im Mail-Body (leer = Default).
        "body_text": str(cfg.get("body_text") or ""),
        "send_day": min(28, max(1, send_day)),
        "transport": transport,
        "smtp": {
            "host": str(smtp.get("host") or ""),
            "port": int(smtp.get("port") or 587),
            "user": str(smtp.get("user") or ""),
            "password": str(smtp.get("password") or ""),
            "starttls": bool(smtp.get("starttls", True)),
        },
    }


def _graph_credentials(tenant: Tenant | None, mailbox_id: str | None = None) -> dict | None:
    """M365-Zugangsdaten fuer den Versand — gezielt das gewaehlte Postfach.

    ``mailbox_id`` waehlt eines der eingerichteten Postfaecher aus; ohne (bzw.
    unbekannte id) das erste vollstaendige m365-Postfach (bisheriges Verhalten).
    """
    m365 = [c for c in resolve_mail_configs(tenant) if c.get("provider") == "m365"]
    if mailbox_id:
        gewaehlt = next((c for c in m365 if str(c.get("id")) == mailbox_id), None)
        if gewaehlt is not None:
            return gewaehlt
    return m365[0] if m365 else None


def previous_month(today: date) -> date:
    """Erster Tag des Vormonats."""
    first = today.replace(day=1)
    return (
        first.replace(year=first.year - 1, month=12)
        if first.month == 1
        else first.replace(month=first.month - 1)
    )


async def _send_graph_mail(
    creds: dict,
    sender: str,
    to: str,
    subject: str,
    body: str,
    attachment_name: str,
    attachment: bytes,
) -> None:
    """Mail mit PDF-Anhang ueber Graph sendMail verschicken (wirft bei Fehler)."""
    import httpx

    async with httpx.AsyncClient(timeout=30) as client:
        token = await _graph_token(client, creds)
        resp = await client.post(
            f"https://graph.microsoft.com/v1.0/users/{sender}/sendMail",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "message": {
                    "subject": subject,
                    "body": {"contentType": "Text", "content": body},
                    "toRecipients": [{"emailAddress": {"address": to}}],
                    "attachments": [
                        {
                            "@odata.type": "#microsoft.graph.fileAttachment",
                            "name": attachment_name,
                            "contentType": "application/pdf",
                            "contentBytes": base64.b64encode(attachment).decode(),
                        }
                    ],
                },
                "saveToSentItems": True,
            },
        )
        if resp.status_code == 403:
            # Haeufigster Konfig-Fehler: die Azure-App darf lesen, aber nicht
            # senden — Mail.Send fehlt (oder eine ApplicationAccessPolicy sperrt
            # das Absender-Postfach). Sprechend statt roher 403.
            raise RuntimeError(
                f"Microsoft 365 verweigert den Versand ueber {sender} (403). "
                "Der Azure-App fehlt vermutlich die Berechtigung 'Mail.Send' "
                "(mit Admin-Zustimmung) oder das Postfach ist per Zugriffsrichtlinie gesperrt."
            )
        resp.raise_for_status()


def _send_smtp_sync(
    smtp: dict,
    sender: str,
    to: str,
    subject: str,
    body: str,
    attachment_name: str,
    attachment: bytes,
) -> None:
    """Mail mit PDF-Anhang ueber einen eigenen Mailserver (SMTP) verschicken."""
    import smtplib
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    msg.set_content(body)
    msg.add_attachment(attachment, maintype="application", subtype="pdf", filename=attachment_name)
    port = int(smtp.get("port") or 587)
    host = smtp["host"]
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=30) as srv:
            if smtp.get("user"):
                srv.login(smtp["user"], smtp.get("password") or "")
            srv.send_message(msg)
        return
    with smtplib.SMTP(host, port, timeout=30) as srv:
        if smtp.get("starttls", True):
            srv.starttls()
        if smtp.get("user"):
            srv.login(smtp["user"], smtp.get("password") or "")
        srv.send_message(msg)


async def _send_mail(
    cfg: dict,
    creds: dict | None,
    to: str,
    subject: str,
    body: str,
    attachment_name: str,
    attachment: bytes,
) -> None:
    """Versand ueber den konfigurierten Weg (Microsoft 365 oder eigener SMTP)."""
    if cfg["transport"] == "smtp":
        import anyio

        await anyio.to_thread.run_sync(
            _send_smtp_sync,
            cfg["smtp"],
            cfg["sender"],
            to,
            subject,
            body,
            attachment_name,
            attachment,
        )
        return
    if creds is None:
        raise RuntimeError("Keine Microsoft-365-Zugangsdaten konfiguriert")
    await _send_graph_mail(creds, cfg["sender"], to, subject, body, attachment_name, attachment)


def resolve_sender(tenant: Tenant | None) -> str:
    """Absenderadresse: expliziter Eintrag, sonst die Adresse des gewaehlten
    M365-Postfachs (dann ist kein separater Absender noetig)."""
    cfg = get_config(tenant)
    if cfg["sender"]:
        return cfg["sender"]
    if cfg["transport"] == "m365":
        creds = _graph_credentials(tenant, cfg["mailbox_id"])
        if creds:
            return str(creds.get("mailbox") or "")
    return ""


def transport_ready(tenant: Tenant | None) -> str | None:
    """Fehlertext, wenn der Versandweg unvollstaendig konfiguriert ist; sonst None."""
    cfg = get_config(tenant)
    if cfg["transport"] == "smtp":
        if not cfg["sender"]:
            return "Kein Absender eingetragen."
        if not cfg["smtp"]["host"]:
            return "Kein SMTP-Server eingetragen."
        return None
    # M365: das gewaehlte Postfach muss existieren und vollstaendig sein.
    if _graph_credentials(tenant, cfg["mailbox_id"]) is None:
        return "Kein Versand-Postfach ausgewaehlt (E-Mail-Postfaecher einrichten)."
    if not resolve_sender(tenant):
        return "Kein Absender: dem gewaehlten Postfach fehlt eine Adresse."
    return None


async def _already_sent(
    db: AsyncSession, tenant_id: uuid.UUID, employee_id: uuid.UUID, ym: date
) -> bool:
    stmt = (
        tenant_scoped(select(TimesheetMailLog.id), TimesheetMailLog, tenant_id)
        .where(TimesheetMailLog.employee_id == employee_id)
        .where(TimesheetMailLog.year_month == ym)
        .where(TimesheetMailLog.status == "sent")
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none() is not None


async def send_previous_month(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    today: date | None = None,
    force_day: bool = False,
) -> dict:
    """Vormonats-Stundenzettel an alle markierten Mitarbeiter verschicken.

    Rueckgabe: Zaehler ``{"sent": n, "skipped": n, "errors": n}``.
    Idempotent — bereits versandte (Mitarbeiter, Monat) werden uebersprungen.
    """
    tenant = await db.get(Tenant, tenant_id)
    cfg = get_config(tenant)
    counters = {"sent": 0, "skipped": 0, "errors": 0}
    if not cfg["enabled"]:
        return counters
    problem = transport_ready(tenant)
    if problem is not None:
        _log.warning("Stundenzettel-Versand (Tenant %s): %s", tenant_id, problem)
        return counters
    creds = _graph_credentials(tenant, cfg["mailbox_id"])
    # Absender: expliziter Eintrag oder die Adresse des gewaehlten Postfachs.
    cfg["sender"] = resolve_sender(tenant)

    heute = today or date.today()
    # Versandtag: vor diesem Tag im Monat geht nichts raus (manueller Anstoss
    # ueber send-now umgeht das, siehe force_day).
    if not force_day and heute.day < cfg["send_day"]:
        return counters

    ym = previous_month(heute)
    month_label = f"{_MONTHS[ym.month - 1]} {ym.year}"

    stmt = (
        tenant_scoped(select(User), User, tenant_id)
        .where(User.monthly_timesheet_email == True)  # noqa: E712 — SQLAlchemy-Ausdruck
        .where(User.is_active == True)  # noqa: E712
        .order_by(User.last_name, User.first_name)
    )
    recipients = list((await db.execute(stmt)).scalars().all())

    for emp in recipients:
        if await _already_sent(db, tenant_id, emp.id, ym):
            continue
        if not emp.email:
            db.add(
                TimesheetMailLog(
                    tenant_id=tenant_id,
                    employee_id=emp.id,
                    year_month=ym,
                    sent_to=None,
                    status="skipped_no_email",
                    detail=None,
                )
            )
            counters["skipped"] += 1
            continue
        try:
            sheet = await report_service.monthly_timesheet(
                db,
                tenant_id,
                ym.year,
                ym.month,
                emp.id,
                has_view_all=True,
                has_view_team=False,
                caller_department_id=None,
                caller_department_ids=None,
            )
            pdf = build_timesheet_pdf(sheet, company_name=tenant.name if tenant else None)
            safe = f"Stundenzettel_{ym.year}-{ym.month:02d}.pdf"
            await _send_mail(
                cfg,
                creds,
                emp.email,
                subject=f"Stundenzettel {month_label}",
                body=(
                    f"Hallo {emp.first_name},\n\n"
                    + (cfg["body_text"] or f"anbei dein Stundenzettel für {month_label}.")
                    + "\n\nDiese E-Mail wurde automatisch von der Zeiterfassung versendet."
                ),
                attachment_name=safe,
                attachment=pdf,
            )
            db.add(
                TimesheetMailLog(
                    tenant_id=tenant_id,
                    employee_id=emp.id,
                    year_month=ym,
                    sent_to=emp.email,
                    status="sent",
                    detail=None,
                )
            )
            counters["sent"] += 1
        except Exception as exc:  # noqa: BLE001 — einzelner Fehler stoppt den Lauf nicht
            _log.warning("Stundenzettel an %s fehlgeschlagen: %s", emp.email, exc)
            db.add(
                TimesheetMailLog(
                    tenant_id=tenant_id,
                    employee_id=emp.id,
                    year_month=ym,
                    sent_to=emp.email,
                    status="error",
                    detail=str(exc)[:500],
                )
            )
            counters["errors"] += 1
    await db.commit()
    return counters


async def send_all_tenants(db: AsyncSession) -> dict:
    """Alle Mandanten mit aktiviertem Versand abarbeiten (fuer den Loop)."""
    tenants = list((await db.execute(select(Tenant.id))).scalars().all())
    total = {"sent": 0, "skipped": 0, "errors": 0}
    for tid in tenants:
        c = await send_previous_month(db, tid)
        for k in total:
            total[k] += c[k]
    return total
