"""Krankmeldungen per E-Mail: Einlesen, Zuordnen, Microsoft-365-Abruf.

Ablauf pro Mail:
1. Dedupe ueber die Message-ID (Mehrfach-Abruf bucht nie doppelt).
2. Absender gegen die Personalakten-E-Mails matchen (case-insensitive).
3. Zeitraum-Vorschlag per Parser (:mod:`zeiterfassung.core.sickmail`).
4. Bei Match: regulaerer SICK_LEAVE-Antrag als SUBMITTED (landet in der
   Antraege-Uebersicht, HR bestaetigt/korrigiert dort). Ohne Match: Eintrag
   bleibt NEW und wird in der Krankmeldungs-Liste manuell zugeordnet.

Der Microsoft-Graph-Abruf ist optional und rein Env-gesteuert — ohne
konfigurierte Zugangsdaten laeuft kein Poller (lokal/Tests unbeeinflusst).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from zeiterfassung.core.sickmail import parse_sick_period
from zeiterfassung.models.absence import RequestType
from zeiterfassung.models.sickmail import SickMailReport
from zeiterfassung.models.tenant import Tenant
from zeiterfassung.models.user import User
from zeiterfassung.policy.context import DayPart

logger = logging.getLogger(__name__)


async def ingest_mail(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    message_id: str,
    sender_email: str,
    subject: str | None,
    body: str | None,
    received_at: datetime,
    sender_name: str | None = None,
) -> SickMailReport | None:
    """Eine Mail verarbeiten. Gibt None zurueck, wenn sie schon bekannt ist."""
    existing = (
        await db.execute(select(SickMailReport).where(SickMailReport.message_id == message_id))
    ).scalar_one_or_none()
    if existing is not None:
        return None

    sender = (sender_email or "").strip().lower()
    # Parser arbeitet mit deutscher Lokalzeit (Abend-Regel, "morgen", ...).
    from zoneinfo import ZoneInfo

    local = received_at.astimezone(ZoneInfo("Europe/Berlin"))
    parsed = parse_sick_period(f"{subject or ''}\n{body or ''}", local.date(), local.hour)

    report = SickMailReport(
        tenant_id=tenant_id,
        message_id=message_id,
        sender_email=sender,
        subject=(subject or "")[:512] or None,
        body_preview=(body or "")[:2000] or None,
        received_at=received_at,
        parsed_date_from=parsed.date_from,
        parsed_date_to=parsed.date_to,
        parsed_hint=(parsed.matched or "")[:255] or None,
        status="NEW",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    # Absender-Match: aktive Personalakte mit exakt dieser E-Mail.
    employee = (
        await db.execute(
            select(User).where(
                User.tenant_id == tenant_id,
                User.email == sender,
                User.is_active == True,  # noqa: E712
            )
        )
    ).scalar_one_or_none()

    db.add(report)
    await db.flush()

    if employee is not None:
        await _link_and_file(db, tenant_id, report, employee.id, assignment="auto")
    elif sender_name:
        # Namens-VORSCHLAG (bucht nichts!): passt der Anzeigename des Absenders
        # eindeutig zu genau einem aktiven Mitarbeiter, wird er in der offenen
        # Meldung vorausgewaehlt — HR bestaetigt weiterhin manuell.
        name = " ".join(sender_name.split()).lower()
        if name:
            candidates = [
                u
                for u in (
                    await db.execute(
                        select(User).where(
                            User.tenant_id == tenant_id,
                            User.is_active == True,  # noqa: E712
                        )
                    )
                )
                .scalars()
                .all()
                if f"{u.first_name} {u.last_name}".lower() == name
                or f"{u.last_name} {u.first_name}".lower() == name
            ]
            if len(candidates) == 1:
                report.employee_id = candidates[0].id

    return report


async def _link_and_file(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    report: SickMailReport,
    employee_id: uuid.UUID,
    assignment: str = "manual",
) -> None:
    """Report einem Mitarbeiter zuordnen und den Krank-Antrag einreichen.

    Kollidiert der Zeitraum mit einer bestehenden Abwesenheit (z.B. schon
    erfasster Krankschreibung), bleibt der Report zugeordnet-aber-NEW —
    HR sieht ihn in der Liste und entscheidet manuell.
    """
    from zeiterfassung.schemas.absence import AbsenceRequestCreate
    from zeiterfassung.services import absence_service

    # Einstellbar (Einstellungen -> Funktionen): direkt genehmigen statt
    # Genehmigungsschritt in den Antraegen.
    tenant = (await db.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one_or_none()
    features = ((tenant.settings or {}).get("features") or {}) if tenant else {}
    auto_approve = features.get("sick_mail_auto_approve", False) is True

    report.employee_id = employee_id
    try:
        # Krankmeldung per Mail ist naturgemäß rückwirkend (man war gestern krank) →
        # den Self-Service-Guard gegen Vergangenheitsdaten via model_construct umgehen
        # (wie der Admin-Weg). Validierung der Fachregeln passiert in create_request.
        request = await absence_service.create_request(
            db,
            tenant_id,
            employee_id,
            AbsenceRequestCreate.model_construct(
                request_type=RequestType.SICK_LEAVE,
                date_from=report.parsed_date_from,
                date_to=report.parsed_date_to,
                day_part=DayPart.FULL,
                hours=None,
                payload=None,
                notes=f"Per E-Mail gemeldet ({report.sender_email})"
                + (f" — erkannt: „{report.parsed_hint}“" if report.parsed_hint else ""),
            ),
            auto_approve=auto_approve,
        )
        report.absence_request_id = request.id
        report.status = "LINKED"
        report.assignment = assignment
    except absence_service.ConflictError as exc:
        logger.info("Krankmail %s: Konflikt mit bestehender Abwesenheit: %s", report.id, exc)
        report.parsed_hint = f"Konflikt: {exc}"[:255]
    report.updated_at = datetime.now(UTC)


async def assign_report(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    report_id: uuid.UUID,
    employee_id: uuid.UUID,
    date_from=None,
    date_to=None,
) -> SickMailReport:
    """Manuelle Zuordnung durch HR — optional mit korrigiertem Zeitraum.

    HR kann den vom Parser vorgeschlagenen Zeitraum vor dem Buchen anpassen
    (falsch erkannte Daten werden so korrigiert, bevor ein Antrag entsteht).
    """
    report = (
        await db.execute(
            select(SickMailReport).where(
                SickMailReport.id == report_id, SickMailReport.tenant_id == tenant_id
            )
        )
    ).scalar_one_or_none()
    if report is None:
        raise LookupError("Krankmeldung nicht gefunden")
    if report.status == "LINKED":
        raise ValueError("Krankmeldung ist bereits zugeordnet")
    if date_from and date_to:
        if date_to < date_from:
            raise ValueError("Bis-Datum liegt vor dem Von-Datum")
        report.parsed_date_from = date_from
        report.parsed_date_to = date_to
        report.parsed_hint = "manuell korrigiert"
    await _link_and_file(db, tenant_id, report, employee_id)
    return report


async def reset_report(
    db: AsyncSession, tenant_id: uuid.UUID, report_id: uuid.UUID, actor_id: uuid.UUID | None
) -> SickMailReport:
    """Zugeordnete/verworfene Meldung zurueck auf 'Offen' setzen.

    Wurde bereits ein Antrag erzeugt, wird er sauber storniert (mit
    Audit-Eintrag) — die Meldung kann danach mit korrigiertem Zeitraum
    neu zugeordnet werden. Buchungen werden nie in-place editiert.
    """
    from zeiterfassung.services import absence_service

    report = (
        await db.execute(
            select(SickMailReport).where(
                SickMailReport.id == report_id, SickMailReport.tenant_id == tenant_id
            )
        )
    ).scalar_one_or_none()
    if report is None:
        raise LookupError("Krankmeldung nicht gefunden")
    if report.status == "NEW":
        raise ValueError("Krankmeldung ist bereits offen")

    if report.absence_request_id is not None:
        try:
            await absence_service.revoke_approval(
                db,
                tenant_id,
                report.absence_request_id,
                actor_id or report.employee_id,
                reason="Krankmeldung zurueckgesetzt (Korrektur)",
            )
        except absence_service.InvalidStatusTransitionError:
            pass  # Antrag war schon abgelehnt/storniert — nichts zu tun.
        report.absence_request_id = None

    report.status = "NEW"
    report.updated_at = datetime.now(UTC)
    return report


async def dismiss_report(
    db: AsyncSession, tenant_id: uuid.UUID, report_id: uuid.UUID
) -> SickMailReport:
    """Fehlgeleitete/irrelevante Mail verwerfen (bleibt zur Doku erhalten)."""
    report = (
        await db.execute(
            select(SickMailReport).where(
                SickMailReport.id == report_id, SickMailReport.tenant_id == tenant_id
            )
        )
    ).scalar_one_or_none()
    if report is None:
        raise LookupError("Krankmeldung nicht gefunden")
    report.status = "DISMISSED"
    report.updated_at = datetime.now(UTC)
    return report


async def delete_report(
    db: AsyncSession, tenant_id: uuid.UUID, report_id: uuid.UUID, actor_id: uuid.UUID | None
) -> None:
    """Meldung endgueltig loeschen (z.B. Spam/Newsletter).

    Ein bereits erzeugter Antrag wird vorher sauber storniert — die Meldung
    selbst verschwindet komplett (bei Spam gibt es nichts zu dokumentieren).
    """
    from zeiterfassung.services import absence_service

    report = (
        await db.execute(
            select(SickMailReport).where(
                SickMailReport.id == report_id, SickMailReport.tenant_id == tenant_id
            )
        )
    ).scalar_one_or_none()
    if report is None:
        raise LookupError("Krankmeldung nicht gefunden")
    if report.absence_request_id is not None:
        try:
            await absence_service.revoke_approval(
                db,
                tenant_id,
                report.absence_request_id,
                actor_id or report.employee_id,
                reason="Krankmeldung geloescht",
            )
        except absence_service.InvalidStatusTransitionError:
            pass
    await db.delete(report)


# ---------------------------------------------------------------------------
# Postfach-Abruf: Konfiguration aus den Einstellungen (DB), Env als Fallback.
# Unterstuetzt Microsoft 365 (Graph API) und klassisches IMAP (z.B. GMX/web.de
# fuer Tests oder Nicht-M365-Postfaecher).
# ---------------------------------------------------------------------------


def _config_complete(cfg: dict) -> bool:
    """True, wenn eine Postfach-Konfiguration alle Pflichtfelder hat."""
    provider = cfg.get("provider")
    if provider == "imap":
        return bool(cfg.get("host") and cfg.get("user") and cfg.get("password"))
    if provider == "m365":
        return all(cfg.get(k) for k in ("tenant_id", "client_id", "client_secret", "mailbox"))
    return False


def get_mailbox_configs(tenant: Tenant | None) -> list[dict]:
    """Alle gepflegten Postfaecher (Liste), inkl. Alt-Format-Uebernahme.

    Frueher gab es genau EIN Postfach unter settings['sickmail'] — das wird
    hier transparent als Eintrag mit id 'legacy' mitgeliefert, bis es beim
    naechsten Speichern in die Liste ueberfuehrt wird.
    """
    if tenant is None:
        return []
    settings = tenant.settings or {}
    boxes = [dict(b) for b in (settings.get("sickmail_boxes") or [])]
    legacy = settings.get("sickmail")
    if legacy and not any(b.get("id") == "legacy" for b in boxes):
        boxes.append({"id": "legacy", **legacy})
    return boxes


def resolve_mail_configs(tenant: Tenant | None) -> list[dict]:
    """Alle VOLLSTAENDIGEN Postfach-Konfigurationen (DB zuerst, Env-Fallback)."""
    from zeiterfassung.config import get_settings

    configs = [c for c in get_mailbox_configs(tenant) if _config_complete(c)]
    if configs:
        return configs
    s = get_settings()
    if s.m365_tenant_id and s.m365_client_id and s.m365_client_secret and s.m365_mailbox:
        return [
            {
                "id": "env",
                "provider": "m365",
                "tenant_id": s.m365_tenant_id,
                "client_id": s.m365_client_id,
                "client_secret": s.m365_client_secret,
                "mailbox": s.m365_mailbox,
            }
        ]
    return []


async def fetch_mailbox(db: AsyncSession) -> int:
    """Neue Mails aus dem konfigurierten Postfach einlesen.

    Rueckgabe: Anzahl neu importierter Meldungen. 0, wenn nicht konfiguriert
    oder die Funktion (sick_mail) in den Einstellungen deaktiviert ist.
    """
    tenant = (await db.execute(select(Tenant))).scalars().first()
    if tenant is None:
        return 0
    features = (tenant.settings or {}).get("features") or {}
    if features.get("sick_mail", True) is False:
        return 0
    configs = [c for c in resolve_mail_configs(tenant) if c.get("intake", True) is not False]
    if not configs:
        return 0

    import asyncio

    messages: list[dict] = []
    for cfg in configs:
        try:
            if cfg["provider"] == "imap":
                messages.extend(await asyncio.to_thread(_fetch_imap_sync, cfg))
            else:
                messages.extend(await _fetch_graph(cfg))
        except Exception:  # noqa: BLE001 — ein kaputtes Postfach blockiert nicht die anderen
            logger.warning("Postfach-Abruf fehlgeschlagen (%s)", cfg.get("id"), exc_info=True)

    imported = 0
    for m in messages:
        report = await ingest_mail(
            db,
            tenant.id,
            message_id=m["message_id"],
            sender_email=m["sender"],
            subject=m["subject"],
            body=m["body"],
            received_at=m["received_at"],
            sender_name=m.get("sender_name"),
        )
        if report is not None:
            imported += 1
    await db.commit()
    return imported


async def test_mail_config(cfg: dict) -> str:
    """Verbindung testen, ohne etwas zu importieren. Wirft bei Fehlern."""
    if cfg.get("provider") == "imap":
        import asyncio

        def _probe() -> str:
            import imaplib

            M = imaplib.IMAP4_SSL(cfg["host"], int(cfg.get("port") or 993))
            try:
                M.login(cfg["user"], cfg["password"])
                typ, data = M.select(cfg.get("folder") or "INBOX", readonly=True)
                if typ != "OK":
                    raise RuntimeError(f"Ordner nicht lesbar: {data}")
                typ, data = M.search(None, "ALL")
                count = len(data[0].split()) if data and data[0] else 0
                return f"Verbindung ok — {count} Mail(s) im Ordner."
            finally:
                try:
                    M.logout()
                except Exception:  # noqa: BLE001
                    pass

        return await asyncio.to_thread(_probe)

    # M365: Token holen + Postfach anfragen
    import httpx

    async with httpx.AsyncClient(timeout=20) as client:
        token = await _graph_token(client, cfg)
        resp = await client.get(
            f"https://graph.microsoft.com/v1.0/users/{cfg['mailbox']}/mailFolders/Inbox",
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        total = resp.json().get("totalItemCount")
        return f"Verbindung ok — {total} Mail(s) im Posteingang."


def _fetch_imap_sync(cfg: dict) -> list[dict]:
    """Ungelesene Mails per IMAP holen (blocking — via to_thread aufrufen)."""
    import email as email_mod
    import imaplib
    from email.header import decode_header, make_header
    from email.utils import parseaddr, parsedate_to_datetime

    out: list[dict] = []
    M = imaplib.IMAP4_SSL(cfg["host"], int(cfg.get("port") or 993))
    try:
        M.login(cfg["user"], cfg["password"])
        M.select(cfg.get("folder") or "INBOX")
        typ, data = M.search(None, "UNSEEN")
        if typ != "OK" or not data or not data[0]:
            return out
        for num in data[0].split()[:25]:
            typ, msgdata = M.fetch(num, "(RFC822)")
            if typ != "OK" or not msgdata or msgdata[0] is None:
                continue
            msg = email_mod.message_from_bytes(msgdata[0][1])
            subject = str(make_header(decode_header(msg.get("Subject") or ""))).strip()
            raw_name, sender = parseaddr(str(make_header(decode_header(msg.get("From") or ""))))
            message_id = (
                msg.get("Message-ID") or ""
            ).strip() or f"imap:{cfg['user']}:{num.decode()}:{subject[:40]}"
            try:
                received = parsedate_to_datetime(msg.get("Date"))
                if received.tzinfo is None:
                    received = received.replace(tzinfo=UTC)
            except Exception:  # noqa: BLE001
                received = datetime.now(UTC)

            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        payload = part.get_payload(decode=True) or b""
                        body = payload.decode(part.get_content_charset() or "utf-8", "replace")
                        break
            else:
                payload = msg.get_payload(decode=True) or b""
                body = payload.decode(msg.get_content_charset() or "utf-8", "replace")

            out.append(
                {
                    "message_id": message_id,
                    "sender": sender,
                    "sender_name": raw_name or None,
                    "subject": subject,
                    "body": body,
                    "received_at": received,
                }
            )
            M.store(num, "+FLAGS", "\\Seen")
    finally:
        try:
            M.logout()
        except Exception:  # noqa: BLE001
            pass
    return out


async def _graph_token(client, cfg: dict) -> str:
    token_resp = await client.post(
        f"https://login.microsoftonline.com/{cfg['tenant_id']}/oauth2/v2.0/token",
        data={
            "grant_type": "client_credentials",
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "scope": "https://graph.microsoft.com/.default",
        },
    )
    token_resp.raise_for_status()
    return token_resp.json()["access_token"]


async def _fetch_graph(cfg: dict) -> list[dict]:
    """Ungelesene Mails aus einem M365-Postfach (Graph API)."""
    import httpx

    out: list[dict] = []
    async with httpx.AsyncClient(timeout=30) as client:
        token = await _graph_token(client, cfg)
        headers = {"Authorization": f"Bearer {token}"}
        base = f"https://graph.microsoft.com/v1.0/users/{cfg['mailbox']}"
        resp = await client.get(
            f"{base}/mailFolders/Inbox/messages",
            params={
                "$filter": "isRead eq false",
                "$top": "25",
                "$select": "id,internetMessageId,subject,bodyPreview,receivedDateTime,from",
            },
            headers=headers,
        )
        resp.raise_for_status()
        for msg in resp.json().get("value", []):
            addr = (msg.get("from") or {}).get("emailAddress") or {}
            sender = addr.get("address") or ""
            received = datetime.fromisoformat(
                (msg.get("receivedDateTime") or "").replace("Z", "+00:00")
            )
            out.append(
                {
                    "message_id": msg.get("internetMessageId") or msg["id"],
                    "sender": sender,
                    "sender_name": addr.get("name") or None,
                    "subject": msg.get("subject"),
                    "body": msg.get("bodyPreview"),
                    "received_at": received,
                }
            )
            await client.patch(
                f"{base}/messages/{msg['id']}", json={"isRead": True}, headers=headers
            )
    return out
