"""Monatlicher Stundenzettel-Versand (Spec 2026-08-07).

Graph-Versand ist gemockt — geprueft werden Empfaengerauswahl, Idempotenz,
Skip ohne E-Mail, Fehlerpfad und die PDF-Erzeugung.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from zeiterfassung.models.timesheet_mail_log import TimesheetMailLog
from zeiterfassung.models.user import User
from zeiterfassung.services import timesheet_mail_service as svc
from zeiterfassung.services.timesheet_pdf import build_timesheet_pdf


def _enable(tenant, sender="zeit@test.de"):
    s = dict(tenant.settings or {})
    s["timesheet_mail"] = {"enabled": True, "sender": sender}
    # M365-Zugangsdaten wie beim Krankmeldungs-Postfach.
    s["sickmail_boxes"] = [
        {
            "id": "b1",
            "provider": "m365",
            "tenant_id": "t",
            "client_id": "c",
            "client_secret": "s",
            "mailbox": "zeit@test.de",
        }
    ]
    tenant.settings = s
    flag_modified(tenant, "settings")


async def _mark(db, employee, enabled=True):
    employee.monthly_timesheet_email = enabled
    await db.commit()


@pytest.mark.asyncio
async def test_versand_und_idempotenz(db, tenant, employee, monkeypatch):
    _enable(tenant)
    await _mark(db, employee)
    sent: list[dict] = []

    async def fake_send(creds, sender, to, subject, body, attachment_name, attachment):
        sent.append({"to": to, "subject": subject, "name": attachment_name, "pdf": attachment})

    monkeypatch.setattr(svc, "_send_graph_mail", fake_send)

    c = await svc.send_previous_month(db, tenant.id, today=date(2026, 8, 7))
    assert c == {"sent": 1, "skipped": 0, "errors": 0}
    assert sent[0]["to"] == employee.email
    assert "Juli 2026" in sent[0]["subject"]
    assert sent[0]["pdf"].startswith(b"%PDF")

    # Zweiter Lauf: nichts mehr zu tun (Log-Eintrag "sent" blockiert).
    c2 = await svc.send_previous_month(db, tenant.id, today=date(2026, 8, 7))
    assert c2 == {"sent": 0, "skipped": 0, "errors": 0}
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_skip_ohne_email_und_fehlerpfad(db, tenant, employee, monkeypatch):
    _enable(tenant)
    await _mark(db, employee)
    employee.email = None
    # Zweiter markierter MA, bei dem der Versand knallt.
    broken = User(
        tenant_id=tenant.id,
        email="kaputt@test.de",
        hashed_password="x",
        first_name="Erika",
        last_name="Fehler",
        monthly_timesheet_email=True,
    )
    db.add(broken)
    await db.commit()

    async def boom(*a, **kw):
        raise RuntimeError("Graph 403")

    monkeypatch.setattr(svc, "_send_graph_mail", boom)

    c = await svc.send_previous_month(db, tenant.id, today=date(2026, 8, 7))
    assert c == {"sent": 0, "skipped": 1, "errors": 1}

    rows = (await db.execute(select(TimesheetMailLog))).scalars().all()
    stats = sorted(r.status for r in rows)
    assert stats == ["error", "skipped_no_email"]
    err = next(r for r in rows if r.status == "error")
    assert "Graph 403" in (err.detail or "")

    # error blockiert NICHT: naechster Lauf versucht es erneut.
    c2 = await svc.send_previous_month(db, tenant.id, today=date(2026, 8, 7))
    assert c2["errors"] == 1


@pytest.mark.asyncio
async def test_deaktiviert_oder_ohne_absender_sendet_nichts(db, tenant, employee, monkeypatch):
    await _mark(db, employee)

    async def fail(*a, **kw):  # darf nie aufgerufen werden
        raise AssertionError("send called")

    monkeypatch.setattr(svc, "_send_graph_mail", fail)
    # Gar keine Konfiguration:
    assert await svc.send_previous_month(db, tenant.id, today=date(2026, 8, 7)) == {
        "sent": 0,
        "skipped": 0,
        "errors": 0,
    }
    # Aktiv, aber ohne Absender:
    s = dict(tenant.settings or {})
    s["timesheet_mail"] = {"enabled": True, "sender": ""}
    tenant.settings = s
    flag_modified(tenant, "settings")
    await db.commit()
    assert await svc.send_previous_month(db, tenant.id, today=date(2026, 8, 7)) == {
        "sent": 0,
        "skipped": 0,
        "errors": 0,
    }


@pytest.mark.asyncio
async def test_nur_markierte_und_aktive(db, tenant, employee, monkeypatch):
    _enable(tenant)
    # employee ist NICHT markiert; dazu ein markierter, aber inaktiver MA.
    inactive = User(
        tenant_id=tenant.id,
        email="weg@test.de",
        hashed_password="x",
        first_name="Ex",
        last_name="Kollege",
        is_active=False,
        monthly_timesheet_email=True,
    )
    db.add(inactive)
    await db.commit()

    async def fail(*a, **kw):
        raise AssertionError("send called")

    monkeypatch.setattr(svc, "_send_graph_mail", fail)
    c = await svc.send_previous_month(db, tenant.id, today=date(2026, 8, 7))
    assert c == {"sent": 0, "skipped": 0, "errors": 0}


def test_previous_month_jahreswechsel():
    assert svc.previous_month(date(2026, 1, 15)) == date(2025, 12, 1)
    assert svc.previous_month(date(2026, 8, 1)) == date(2026, 7, 1)


@pytest.mark.asyncio
async def test_pdf_erzeugung(db, tenant, employee):
    from zeiterfassung.services import report_service

    sheet = await report_service.monthly_timesheet(
        db,
        tenant.id,
        2026,
        7,
        employee.id,
        has_view_all=True,
        has_view_team=False,
        caller_department_id=None,
        caller_department_ids=None,
    )
    pdf = build_timesheet_pdf(sheet, company_name="Testfirma GmbH")
    assert pdf.startswith(b"%PDF")
    assert len(pdf) > 1000


@pytest.mark.asyncio
async def test_eigener_mailtext(db, tenant, employee, monkeypatch):
    _enable(tenant)
    s = dict(tenant.settings or {})
    s["timesheet_mail"]["body_text"] = "hier dein Zettel. Fragen ans Büro."
    tenant.settings = s
    flag_modified(tenant, "settings")
    await _mark(db, employee)
    bodies: list[str] = []

    async def fake_send(creds, sender, to, subject, body, attachment_name, attachment):
        bodies.append(body)

    monkeypatch.setattr(svc, "_send_graph_mail", fake_send)
    await svc.send_previous_month(db, tenant.id, today=date(2026, 8, 7))
    assert bodies and "hier dein Zettel. Fragen ans Büro." in bodies[0]
    assert bodies[0].startswith("Hallo Max")


@pytest.mark.asyncio
async def test_versandtag_wird_beachtet(db, tenant, employee, monkeypatch):
    """Vor dem eingestellten Versandtag geht nichts raus, danach schon."""
    _enable(tenant)
    s = dict(tenant.settings or {})
    s["timesheet_mail"]["send_day"] = 5
    tenant.settings = s
    flag_modified(tenant, "settings")
    await _mark(db, employee)
    sent: list[str] = []

    async def fake_send(creds, sender, to, subject, body, attachment_name, attachment):
        sent.append(to)

    monkeypatch.setattr(svc, "_send_graph_mail", fake_send)

    # 3. des Monats -> noch zu frueh
    c = await svc.send_previous_month(db, tenant.id, today=date(2026, 8, 3))
    assert c == {"sent": 0, "skipped": 0, "errors": 0}
    assert sent == []

    # 5. -> jetzt raus
    c = await svc.send_previous_month(db, tenant.id, today=date(2026, 8, 5))
    assert c["sent"] == 1


@pytest.mark.asyncio
async def test_manueller_anstoss_ignoriert_versandtag(db, tenant, employee, monkeypatch):
    _enable(tenant)
    s = dict(tenant.settings or {})
    s["timesheet_mail"]["send_day"] = 20
    tenant.settings = s
    flag_modified(tenant, "settings")
    await _mark(db, employee)

    async def fake_send(*a, **kw):
        return None

    monkeypatch.setattr(svc, "_send_graph_mail", fake_send)
    c = await svc.send_previous_month(db, tenant.id, today=date(2026, 8, 3), force_day=True)
    assert c["sent"] == 1


@pytest.mark.asyncio
async def test_smtp_transport(db, tenant, employee, monkeypatch):
    """Bei transport=smtp laeuft der Versand ueber den eigenen Mailserver."""
    _enable(tenant)
    s = dict(tenant.settings or {})
    s["timesheet_mail"]["transport"] = "smtp"
    s["timesheet_mail"]["smtp"] = {
        "host": "mail.test.de",
        "port": 587,
        "user": "u",
        "password": "p",
    }
    tenant.settings = s
    flag_modified(tenant, "settings")
    await _mark(db, employee)
    calls: list[dict] = []

    def fake_smtp(smtp, sender, to, subject, body, attachment_name, attachment):
        calls.append({"host": smtp["host"], "to": to, "pdf": attachment[:4]})

    async def boom_graph(*a, **kw):
        raise AssertionError("Graph darf bei SMTP nicht genutzt werden")

    monkeypatch.setattr(svc, "_send_smtp_sync", fake_smtp)
    monkeypatch.setattr(svc, "_send_graph_mail", boom_graph)

    c = await svc.send_previous_month(db, tenant.id, today=date(2026, 8, 7))
    assert c["sent"] == 1
    assert calls[0]["host"] == "mail.test.de"
    assert calls[0]["pdf"] == b"%PDF"


@pytest.mark.asyncio
async def test_unvollstaendige_smtp_konfig_sendet_nicht(db, tenant, employee, monkeypatch):
    _enable(tenant)
    s = dict(tenant.settings or {})
    s["timesheet_mail"]["transport"] = "smtp"
    s["timesheet_mail"]["smtp"] = {"host": "", "port": 587}
    tenant.settings = s
    flag_modified(tenant, "settings")
    await _mark(db, employee)

    def fail(*a, **kw):
        raise AssertionError("darf nicht senden")

    monkeypatch.setattr(svc, "_send_smtp_sync", fail)
    c = await svc.send_previous_month(db, tenant.id, today=date(2026, 8, 7))
    assert c == {"sent": 0, "skipped": 0, "errors": 0}
    assert svc.transport_ready(tenant) == "Kein SMTP-Server eingetragen."


@pytest.mark.asyncio
async def test_versand_ueber_gewaehltes_postfach(db, tenant, employee, monkeypatch):
    """mailbox_id waehlt gezielt ein Postfach; Absender faellt auf dessen Adresse."""
    s = dict(tenant.settings or {})
    s["timesheet_mail"] = {"enabled": True, "mailbox_id": "versand"}  # KEIN sender
    s["sickmail_boxes"] = [
        {
            "id": "krank",
            "provider": "m365",
            "tenant_id": "t",
            "client_id": "c",
            "client_secret": "s",
            "mailbox": "krank@test.de",
            "intake": True,
        },
        {
            "id": "versand",
            "provider": "m365",
            "tenant_id": "t",
            "client_id": "c",
            "client_secret": "s",
            "mailbox": "lohn@test.de",
            "intake": False,
        },
    ]
    tenant.settings = s
    flag_modified(tenant, "settings")
    await _mark(db, employee)

    captured: list[dict] = []

    async def fake_send(creds, sender, to, subject, body, attachment_name, attachment):
        captured.append({"sender": sender, "mailbox": creds.get("mailbox")})

    monkeypatch.setattr(svc, "_send_graph_mail", fake_send)
    c = await svc.send_previous_month(db, tenant.id, today=date(2026, 8, 7))
    assert c["sent"] == 1
    # Gewaehltes Postfach 'versand', Absender = dessen Adresse (kein expliziter).
    assert captured[0]["mailbox"] == "lohn@test.de"
    assert captured[0]["sender"] == "lohn@test.de"


def test_transport_ready_meldet_fehlendes_postfach(tenant):
    from types import SimpleNamespace

    t = SimpleNamespace(settings={"timesheet_mail": {"enabled": True, "mailbox_id": "gibtsnicht"}})
    problem = svc.transport_ready(t)
    assert problem is not None and "Postfach" in problem


def test_intake_flag_steuert_krankmail_abruf():
    """fetch_mailbox liest nur Postfaecher mit intake != False."""
    from types import SimpleNamespace

    from zeiterfassung.services import sickmail_service as sm

    tenant = SimpleNamespace(
        settings={
            "sickmail_boxes": [
                {
                    "id": "a",
                    "provider": "imap",
                    "host": "h",
                    "user": "u",
                    "password": "p",
                    "intake": True,
                },
                {
                    "id": "b",
                    "provider": "imap",
                    "host": "h",
                    "user": "u",
                    "password": "p",
                    "intake": False,
                },
            ]
        }
    )
    lesbar = [c for c in sm.resolve_mail_configs(tenant) if c.get("intake", True) is not False]
    ids = {c["id"] for c in lesbar}
    assert ids == {"a"}  # 'b' (Versand-Postfach) wird NICHT abgerufen
