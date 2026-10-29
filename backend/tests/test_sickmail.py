"""Tests: Krankmeldung per E-Mail (Parser + Einlesen/Zuordnen)."""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import select

from zeiterfassung.core.sickmail import parse_sick_period
from zeiterfassung.models.absence import ApprovalRequest, RequestStatus, RequestType
from zeiterfassung.services.sickmail_service import assign_report, dismiss_report, ingest_mail

RECEIVED = date(2026, 7, 6)  # ein Montag


class TestParser:
    def test_bis_datum(self):
        p = parse_sick_period("bin krankgeschrieben bis 10.07.2026", RECEIVED)
        assert (p.date_from, p.date_to) == (RECEIVED, date(2026, 7, 10))

    def test_bis_datum_ohne_jahr(self):
        p = parse_sick_period("Krankmeldung bis 09.07.", RECEIVED)
        assert p.date_to == date(2026, 7, 9)

    def test_bis_einschliesslich_wochentag(self):
        p = parse_sick_period("bin bis einschließlich Freitag krank", RECEIVED)
        assert p.date_to == date(2026, 7, 10)  # Freitag derselben Woche

    def test_diese_woche(self):
        p = parse_sick_period("ich falle diese Woche aus", RECEIVED)
        assert p.date_to == date(2026, 7, 10)

    def test_n_tage(self):
        p = parse_sick_period("laut Arzt 3 Tage", RECEIVED)
        assert p.date_to == date(2026, 7, 8)

    def test_freitext_ohne_zeitraum_default_1_tag(self):
        p = parse_sick_period("Moin, mir geht's nicht gut, bin raus. LG", RECEIVED)
        assert (p.date_from, p.date_to) == (RECEIVED, RECEIVED)
        assert p.matched is None

    def test_jahreswechsel(self):
        p = parse_sick_period("bis 03.01.", date(2026, 12, 29))
        assert p.date_to == date(2027, 1, 3)


class TestIngest:
    async def test_match_erzeugt_antrag(self, db, tenant, employee):
        report = await ingest_mail(
            db,
            tenant.id,
            message_id="<m1@test>",
            sender_email=employee.email.upper(),  # Case egal
            subject="Krankmeldung",
            body="bin krank bis 08.07.2026",
            received_at=datetime(2026, 7, 6, 7, 30, tzinfo=UTC),
        )
        assert report is not None and report.status == "LINKED"
        assert report.employee_id == employee.id
        req = (
            await db.execute(
                select(ApprovalRequest).where(ApprovalRequest.id == report.absence_request_id)
            )
        ).scalar_one()
        assert req.request_type == RequestType.SICK_LEAVE
        assert req.status == RequestStatus.SUBMITTED
        assert (req.date_from, req.date_to) == (date(2026, 7, 6), date(2026, 7, 8))

    async def test_unbekannter_absender_bleibt_new(self, db, tenant, employee):
        report = await ingest_mail(
            db,
            tenant.id,
            message_id="<m2@test>",
            sender_email="fremd@anderswo.de",
            subject="krank",
            body="",
            received_at=datetime(2026, 7, 6, 8, 0, tzinfo=UTC),
        )
        assert report is not None and report.status == "NEW" and report.employee_id is None
        # ... und kann manuell zugeordnet werden:
        report = await assign_report(db, tenant.id, report.id, employee.id)
        assert report.status == "LINKED" and report.absence_request_id is not None

    async def test_dedupe_gleiche_message_id(self, db, tenant, employee):
        kw = dict(
            message_id="<m3@test>",
            sender_email="x@y.de",
            subject="s",
            body="",
            received_at=datetime(2026, 7, 6, 8, 0, tzinfo=UTC),
        )
        first = await ingest_mail(db, tenant.id, **kw)
        second = await ingest_mail(db, tenant.id, **kw)
        assert first is not None and second is None

    async def test_dismiss(self, db, tenant, employee):
        report = await ingest_mail(
            db,
            tenant.id,
            message_id="<m4@test>",
            sender_email="spam@spam.de",
            subject="Newsletter",
            body="",
            received_at=datetime(2026, 7, 6, 9, 0, tzinfo=UTC),
        )
        report = await dismiss_report(db, tenant.id, report.id)
        assert report.status == "DISMISSED"


class TestKorrektur:
    async def test_assign_mit_korrigiertem_zeitraum(self, db, tenant, employee):
        from zeiterfassung.services.sickmail_service import assign_report

        r = await ingest_mail(
            db,
            tenant.id,
            message_id="<k1@test>",
            sender_email="wer@wo.de",
            subject="krank",
            body="bis 08.07.2026",
            received_at=datetime(2026, 7, 6, 8, 0, tzinfo=UTC),
        )
        # HR korrigiert: tatsaechlich nur 1 Tag
        r = await assign_report(
            db, tenant.id, r.id, employee.id, date_from=date(2026, 7, 6), date_to=date(2026, 7, 6)
        )
        assert r.status == "LINKED" and r.parsed_hint == "manuell korrigiert"
        req = (
            await db.execute(
                select(ApprovalRequest).where(ApprovalRequest.id == r.absence_request_id)
            )
        ).scalar_one()
        assert (req.date_from, req.date_to) == (date(2026, 7, 6), date(2026, 7, 6))

    async def test_reset_storniert_antrag_und_reopnet(self, db, tenant, employee):
        from zeiterfassung.services.sickmail_service import reset_report

        r = await ingest_mail(
            db,
            tenant.id,
            message_id="<k2@test>",
            sender_email=employee.email,
            subject="Krank",
            body="bis 10.07.2026",
            received_at=datetime(2026, 7, 6, 8, 0, tzinfo=UTC),
        )
        assert r.status == "LINKED"
        req_id = r.absence_request_id
        r = await reset_report(db, tenant.id, r.id, None)
        assert r.status == "NEW" and r.absence_request_id is None
        req = (
            await db.execute(select(ApprovalRequest).where(ApprovalRequest.id == req_id))
        ).scalar_one()
        assert req.status == RequestStatus.CANCELLED
        # ...und mit korrigiertem Zeitraum neu zuordenbar (kein Konflikt mehr):
        from zeiterfassung.services.sickmail_service import assign_report

        r = await assign_report(
            db, tenant.id, r.id, employee.id, date_from=date(2026, 7, 7), date_to=date(2026, 7, 8)
        )
        assert r.status == "LINKED"

    async def test_reset_dismissed_wiederherstellen(self, db, tenant, employee):
        from zeiterfassung.services.sickmail_service import reset_report

        r = await ingest_mail(
            db,
            tenant.id,
            message_id="<k3@test>",
            sender_email="spam@x.de",
            subject="x",
            body="",
            received_at=datetime(2026, 7, 6, 8, 0, tzinfo=UTC),
        )
        r = await dismiss_report(db, tenant.id, r.id)
        r = await reset_report(db, tenant.id, r.id, None)
        assert r.status == "NEW"


class TestParserMorgenAbend:
    def test_morgen(self):
        p = parse_sick_period("bin morgen krank", RECEIVED, hour=8)
        assert (p.date_from, p.date_to) == (date(2026, 7, 7), date(2026, 7, 7))

    def test_uebermorgen(self):
        p = parse_sick_period("bin übermorgen beim Arzt und krank", RECEIVED, hour=8)
        assert (p.date_from, p.date_to) == (date(2026, 7, 8), date(2026, 7, 8))

    def test_morgen_und_uebermorgen(self):
        p = parse_sick_period("Ich bin morgen und übermorgen krank", RECEIVED, hour=17)
        assert (p.date_from, p.date_to) == (date(2026, 7, 7), date(2026, 7, 8))

    def test_guten_morgen_ist_kein_morgen(self):
        p = parse_sick_period("Guten Morgen, ich bin krank", RECEIVED, hour=8)
        assert (p.date_from, p.date_to) == (RECEIVED, RECEIVED)

    def test_abends_ohne_angabe_gilt_morgen(self):
        p = parse_sick_period("bin krank, schaffe es nicht", RECEIVED, hour=19)
        assert (p.date_from, p.date_to) == (date(2026, 7, 7), date(2026, 7, 7))
        assert "morgen" in (p.matched or "")

    def test_morgens_ohne_angabe_gilt_heute(self):
        p = parse_sick_period("bin krank", RECEIVED, hour=7)
        assert (p.date_from, p.date_to) == (RECEIVED, RECEIVED)

    def test_heute_schaltet_abendregel_ab(self):
        p = parse_sick_period("bin heute krank geworden", RECEIVED, hour=20)
        assert (p.date_from, p.date_to) == (RECEIVED, RECEIVED)

    def test_abends_n_tage_ab_morgen(self):
        p = parse_sick_period("bin die nächsten 2 Tage raus", RECEIVED, hour=18)
        assert (p.date_from, p.date_to) == (date(2026, 7, 7), date(2026, 7, 8))

    def test_ab_morgen_bis_freitag(self):
        p = parse_sick_period("ab morgen bis Freitag krankgeschrieben", RECEIVED, hour=10)
        assert (p.date_from, p.date_to) == (date(2026, 7, 7), date(2026, 7, 10))


class TestNamensVorschlag:
    async def test_eindeutiger_name_wird_vorgeschlagen(self, db, tenant, employee):
        r = await ingest_mail(
            db,
            tenant.id,
            message_id="<n1@test>",
            sender_email="privat@gmail.com",
            subject="krank",
            body="bis Freitag",
            received_at=datetime(2026, 7, 6, 8, 0, tzinfo=UTC),
            sender_name=f"{employee.first_name} {employee.last_name}",
        )
        # Vorschlag: Mitarbeiter gesetzt, aber NICHT gebucht.
        assert r.status == "NEW"
        assert r.employee_id == employee.id
        assert r.absence_request_id is None

    async def test_unbekannter_name_kein_vorschlag(self, db, tenant, employee):
        r = await ingest_mail(
            db,
            tenant.id,
            message_id="<n2@test>",
            sender_email="privat2@gmail.com",
            subject="krank",
            body="",
            received_at=datetime(2026, 7, 6, 8, 0, tzinfo=UTC),
            sender_name="Tradelord Kapitän",
        )
        assert r.status == "NEW" and r.employee_id is None


class TestAutoApprove:
    async def test_auto_approve_wenn_eingeschaltet(self, db, tenant, employee):
        tenant.settings = {**(tenant.settings or {}), "features": {"sick_mail_auto_approve": True}}
        await db.flush()
        r = await ingest_mail(
            db,
            tenant.id,
            message_id="<aa1@test>",
            sender_email=employee.email,
            subject="Krank",
            body="bis 09.07.2026",
            received_at=datetime(2026, 7, 6, 8, 0, tzinfo=UTC),
        )
        req = (
            await db.execute(
                select(ApprovalRequest).where(ApprovalRequest.id == r.absence_request_id)
            )
        ).scalar_one()
        assert req.status == RequestStatus.APPROVED
