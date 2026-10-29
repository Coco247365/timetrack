"""Sonderurlaub + Überstundenabbau (Spec 2026-08-11).

Policy-Regeln, Tageszeilen der Überstunden-Engine, Grund-Pflicht und
Stundenzettel-Labels.
"""

from __future__ import annotations

from datetime import date

import pytest

from zeiterfassung.models.absence import (
    ApprovalRequest,
    DayPart,
    RequestStatus,
    RequestType,
)
from zeiterfassung.policy import overtime as pol
from zeiterfassung.schemas.absence import AbsenceRequestCreate
from zeiterfassung.services import absence_service, report_service
from zeiterfassung.services.absence_service import ConflictError


def _facts(**kw) -> pol.DayFacts:
    base = dict(
        day=date(2026, 8, 5),
        weekend=False,
        holiday=False,
        coverage=0.0,
        net_minutes=0,
        has_timesheet=False,
        worked=False,
        is_auto=True,
        is_past=True,
        is_today_or_past=True,
        within_cutoff=True,
    )
    base.update(kw)
    return pol.DayFacts(**base)


def test_abbau_tag_zaehlt_minus_aber_nicht_unentschuldigt():
    # Auto-Erfasser, kein Tagesbogen (Auto-Fill überspringt Abbau-Tage):
    # ohne reduction gäbe es KEINEN Beitrag — mit reduction zählt das Minus.
    res = pol.classify_day(_facts(reduction=1.0), 480)
    assert res is not None
    assert res.delta_minutes == -480
    assert res.absent is False


def test_ohne_abbau_bleibt_unentschuldigt():
    # Stempler ohne Bogen in der Vergangenheit: weiterhin unentschuldigt (rot).
    res = pol.classify_day(_facts(is_auto=False), 480)
    assert res is not None
    assert res.absent is True


def test_abbau_ohne_bogen_bei_auto_sonst_kein_beitrag():
    # Regressionsschutz: Auto-Erfasser ohne Bogen und ohne Abbau -> None.
    assert pol.classify_day(_facts(), 480) is None


@pytest.mark.asyncio
async def test_sonderurlaub_braucht_grund(db, tenant, employee):
    data = AbsenceRequestCreate.model_construct(
        request_type=RequestType.SPECIAL_LEAVE,
        date_from=date(2026, 8, 12),
        date_to=date(2026, 8, 12),
        day_part=DayPart.FULL,
        hours=None,
        sick_status=None,
        notes="   ",
        payload=None,
    )
    with pytest.raises(ConflictError, match="Begründung"):
        await absence_service.create_request(db, tenant.id, employee.id, data)

    data2 = AbsenceRequestCreate.model_construct(
        request_type=RequestType.SPECIAL_LEAVE,
        date_from=date(2026, 8, 12),
        date_to=date(2026, 8, 12),
        day_part=DayPart.FULL,
        hours=None,
        sick_status=None,
        notes="Hochzeit",
        payload=None,
    )
    req = await absence_service.create_request(db, tenant.id, employee.id, data2)
    assert req.request_type == RequestType.SPECIAL_LEAVE


@pytest.mark.asyncio
async def test_stundenzettel_labels_und_soll(db, tenant, employee):
    # Mi 2026-07-15 Sonderurlaub (Soll gedeckt), Do 2026-07-16 Überstundenabbau
    # (Soll bleibt, Tag zeigt Minus).
    db.add(
        ApprovalRequest(
            tenant_id=tenant.id,
            employee_id=employee.id,
            request_type=RequestType.SPECIAL_LEAVE,
            status=RequestStatus.APPROVED,
            date_from=date(2026, 7, 15),
            date_to=date(2026, 7, 15),
            days_count=1.0,
            notes="Hochzeit",
        )
    )
    db.add(
        ApprovalRequest(
            tenant_id=tenant.id,
            employee_id=employee.id,
            request_type=RequestType.OVERTIME_REDUCTION,
            status=RequestStatus.APPROVED,
            date_from=date(2026, 7, 16),
            date_to=date(2026, 7, 16),
            days_count=1.0,
        )
    )
    await db.commit()
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
    by_date = {d.date: d for d in sheet.days}
    sonder = by_date[date(2026, 7, 15)]
    abbau = by_date[date(2026, 7, 16)]
    assert sonder.absence == "Sonderurlaub"
    assert sonder.soll_minutes == 0  # Soll gedeckt (zählt wie Arbeit)
    assert abbau.absence == "Überst.-Abbau"
    assert abbau.soll_minutes > 0  # Soll bleibt offen -> Minus = Abbau
    # Sonderurlaub landet NICHT in den Urlaub/Krank-Summen.
    assert sheet.summary.urlaub_tage == 0
    assert sheet.summary.krank_tage == 0


@pytest.mark.asyncio
async def test_unbezahlt_deckt_soll_ohne_verguetung(db, tenant, employee):
    """Unbezahlt (entschuldigt): 0 Std, kein Minus aufs Konto, eigenes Label."""
    db.add(
        ApprovalRequest(
            tenant_id=tenant.id,
            employee_id=employee.id,
            request_type=RequestType.UNPAID_LEAVE,
            status=RequestStatus.APPROVED,
            date_from=date(2026, 7, 20),
            date_to=date(2026, 7, 20),
            days_count=1.0,
        )
    )
    await db.commit()
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
    tag = next(d for d in sheet.days if d.date == date(2026, 7, 20))
    assert tag.absence == "Unbezahlt"
    assert tag.soll_minutes == 0  # Soll gedeckt -> kein Minus, keine Fehlzeit
    # Zaehlt in KEINEN Urlaub/Krank-Block (keine Verguetung dahinter).
    assert sheet.summary.urlaub_tage == 0
    assert sheet.summary.krank_tage == 0


def test_unbezahlt_kein_unentschuldigt():
    """Unbezahlt deckt den Tag -> nie als unentschuldigtes Fehlen markiert."""
    res = pol.classify_day(_facts(coverage=1.0), 480)
    assert res is None or res.absent is False
