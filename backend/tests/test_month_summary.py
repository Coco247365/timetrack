"""Monats-Summen des Arbeitszeitkontos (Ist / effektives Soll / Saldo).

Für die Kalenderköpfe (Arbeitszeit-Kalender, „Mein Monat"): ein Monat wird
tagesgenau über die Konto-Policy summiert — Feiertage und Abwesenheiten
reduzieren das Soll, unentschuldigte Fehltage (NFC/Web) zählen als Minus,
Zukunftstage zählen nicht („Soll bis heute" im laufenden Monat).

„Heute" wird fest übergeben, damit die Ergebnisse systemuhr-unabhängig sind.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from zeiterfassung.core.security import create_access_token
from zeiterfassung.models.absence import (
    ApprovalRequest,
    DayPart,
    RequestStatus,
    RequestType,
)
from zeiterfassung.models.clock import DailyTimesheet
from zeiterfassung.services import overtime_service as ot

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _ts(tenant_id, emp_id, d, net):
    return DailyTimesheet(
        tenant_id=tenant_id,
        employee_id=emp_id,
        work_date=d,
        start_time=datetime(d.year, d.month, d.day, 8, 0, tzinfo=UTC),
        break_minutes=0,
        net_work_minutes=net,
        gross_work_minutes=net,
        overtime_minutes=0,
        created_at=NOW,
        updated_at=NOW,
    )


def _vacation(tenant_id, emp_id, d):
    return ApprovalRequest(
        tenant_id=tenant_id,
        employee_id=emp_id,
        request_type=RequestType.VACATION,
        status=RequestStatus.APPROVED,
        date_from=d,
        date_to=d,
        day_part=DayPart.FULL,
        days_count=Decimal("1"),
        created_at=NOW,
        updated_at=NOW,
    )


class TestMonthSummaryService:
    async def test_feiertage_und_urlaub_reduzieren_soll(self, db, tenant, employee) -> None:
        # April 2026 (HH): 3.4. Karfreitag, 6.4. Ostermontag, 4./5. WE.
        # Gearbeitet 1.4. (500) und 2.4. (460), Urlaub 7.4., heute = 8.4. ohne
        # Stempel (heute zählt nie als Fehltag) → Soll 960, Ist 960, Saldo 0.
        employee.time_capture = "nfc"
        db.add(_ts(tenant.id, employee.id, date(2026, 4, 1), 500))
        db.add(_ts(tenant.id, employee.id, date(2026, 4, 2), 460))
        db.add(_vacation(tenant.id, employee.id, date(2026, 4, 7)))
        await db.flush()

        s = await ot.month_summary(db, tenant.id, employee, 2026, 4, today=date(2026, 4, 8))
        assert s["ist_minutes"] == 960
        assert s["soll_minutes"] == 960
        assert s["saldo_minutes"] == 0

    async def test_unentschuldigter_fehltag_zaehlt_ins_soll(self, db, tenant, employee) -> None:
        # Wie oben, aber heute = 9.4.: der 8.4. (Mi) ist ein vergangener Werktag
        # ohne Stempel/Abwesenheit → Soll +480, Saldo −480.
        employee.time_capture = "nfc"
        db.add(_ts(tenant.id, employee.id, date(2026, 4, 1), 500))
        db.add(_ts(tenant.id, employee.id, date(2026, 4, 2), 460))
        db.add(_vacation(tenant.id, employee.id, date(2026, 4, 7)))
        await db.flush()

        s = await ot.month_summary(db, tenant.id, employee, 2026, 4, today=date(2026, 4, 9))
        assert s["ist_minutes"] == 960
        assert s["soll_minutes"] == 1440
        assert s["saldo_minutes"] == -480

    async def test_monat_vor_stichtag_ist_null(self, db, tenant, employee) -> None:
        employee.counting_start_date = date(2026, 5, 1)
        db.add(_ts(tenant.id, employee.id, date(2026, 4, 1), 500))
        await db.flush()

        s = await ot.month_summary(db, tenant.id, employee, 2026, 4, today=date(2026, 6, 1))
        assert s["ist_minutes"] == 0
        assert s["soll_minutes"] == 0
        assert s["saldo_minutes"] == 0


# ---------------------------------------------------------------------------
# API: GET /overtime/month-summary
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def app_with_db(engine):
    from zeiterfassung.api import deps
    from zeiterfassung.main import create_app

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async def override_get_db():
        async with factory() as session:
            yield session

    app = create_app()
    app.dependency_overrides[deps.get_db] = override_get_db
    yield app
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client(app_with_db):
    async with AsyncClient(transport=ASGITransport(app=app_with_db), base_url="http://test") as c:
        yield c


async def _headers(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    permissions: list[str],
    employee_id: uuid.UUID | None = None,
) -> dict[str, str]:
    from tests.conftest import ensure_login_account

    acc = await ensure_login_account(db, tenant_id, employee_id=employee_id)
    tok = create_access_token(
        user_id=str(acc.id),
        tenant_id=str(tenant_id),
        permissions=permissions,
        department_id=None,
        role_type="mitarbeiter",
        data_scope="self",
        employee_id=str(employee_id) if employee_id else None,
    )
    return {"Authorization": f"Bearer {tok}"}


class TestMonthSummaryApi:
    async def test_selbstzugriff_erlaubt(self, db, client, tenant, employee) -> None:
        await db.commit()
        r = await client.get(
            f"/api/v1/overtime/month-summary?employee_id={employee.id}&year=2026&month=4",
            headers=await _headers(db, tenant.id, [], employee_id=employee.id),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert set(body) >= {"year", "month", "ist_minutes", "soll_minutes", "saldo_minutes"}
        assert body["year"] == 2026 and body["month"] == 4

    async def test_fremdzugriff_ohne_recht_403(self, db, client, tenant, employee) -> None:
        await db.commit()
        r = await client.get(
            f"/api/v1/overtime/month-summary?employee_id={employee.id}&year=2026&month=4",
            headers=await _headers(db, tenant.id, [], employee_id=uuid.uuid4()),
        )
        assert r.status_code == 403, r.text

    async def test_fremdzugriff_mit_admin_leserecht(self, db, client, tenant, employee) -> None:
        await db.commit()
        r = await client.get(
            f"/api/v1/overtime/month-summary?employee_id={employee.id}&year=2026&month=4",
            headers=await _headers(db, tenant.id, ["users:read_all"]),
        )
        assert r.status_code == 200, r.text


class TestBruttosollzeit:
    """Bruttosollzeit = Arbeitstage × Tagessoll VOR Fehlzeiten-Abzug (Alt-System-
    Begriff für den Abgleich): Brutto − Abwesenheits-Anteile = effektives Soll."""

    async def test_brutto_enthaelt_urlaubstag(self, db, tenant, employee) -> None:
        # Wie oben: 1./2.4. gearbeitet, 7.4. Urlaub, heute 8.4. —
        # effektives Soll 960, Brutto zusätzlich der Urlaubstag: 1440.
        employee.time_capture = "nfc"
        db.add(_ts(tenant.id, employee.id, date(2026, 4, 1), 500))
        db.add(_ts(tenant.id, employee.id, date(2026, 4, 2), 460))
        db.add(_vacation(tenant.id, employee.id, date(2026, 4, 7)))
        await db.flush()

        s = await ot.month_summary(db, tenant.id, employee, 2026, 4, today=date(2026, 4, 8))
        assert s["soll_minutes"] == 960
        # + Urlaubstag (480) + die beiden Werktags-Feiertage 3.4./6.4. (je 480):
        # gleiche Definition wie Stundenzettel/Monatsabgleich (Altsystem-Kette).
        # Wochenenden zählen nicht.
        assert s["brutto_soll_minutes"] == 1440 + 960

    async def test_ohne_fehlzeiten_ist_brutto_gleich_soll(self, db, tenant, employee) -> None:
        employee.time_capture = "nfc"
        db.add(_ts(tenant.id, employee.id, date(2026, 4, 1), 480))
        db.add(_ts(tenant.id, employee.id, date(2026, 4, 2), 480))
        await db.flush()

        s = await ot.month_summary(db, tenant.id, employee, 2026, 4, today=date(2026, 4, 2))
        # Heute (2.4.) ist gearbeitet → zählt erst morgen, auch im Brutto.
        assert s["soll_minutes"] == s["brutto_soll_minutes"] == 480

    async def test_gestempelter_zukunftstag_haelt_invariante(self, db, tenant, employee) -> None:
        """Randfall: ein Tagesbogen in der Zukunft zählt ins effektive Soll —
        das Brutto muss ihn mitzählen, sonst wäre Brutto < effektiv."""
        employee.time_capture = "nfc"
        db.add(_ts(tenant.id, employee.id, date(2026, 4, 1), 480))
        db.add(_ts(tenant.id, employee.id, date(2026, 4, 15), 480))  # Zukunft
        await db.flush()

        s = await ot.month_summary(db, tenant.id, employee, 2026, 4, today=date(2026, 4, 2))
        assert s["brutto_soll_minutes"] >= s["soll_minutes"]
        assert s["brutto_soll_minutes"] == s["soll_minutes"]  # keine Fehlzeiten
