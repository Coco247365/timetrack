"""Beta-Fall: above_cap -> keine Staffel, Auszahlung aus dem Abschluss (LA 012).

Erwartung aus echter Datei 2026-06_BETA_LODAS.txt, PN 42:
011=168 (Soll 176 - 8 Urlaub), 131=8 (1 Urlaubstag a 8h), 012=22,08 (Ueberlauf).

Mitarbeiter above_cap (cap 20h), Wochenplan Mo-Fr 8h (weekday_target_minutes
[480,480,480,480,480,0,0]), PNr "00042". Juni 2026: jeden Arbeitstag exakt 8h
stempeln (08:00-16:30, BREAK 12:00-12:30) AUSSER 1 Urlaubstag (Mo 29.06.);
zusaetzlich an 2 Tagen bewusst 9h und an 1 Tag 7h stempeln — diese taeglichen
Mehr-/Minderzeiten sind fuer den Test irrelevant, weil die Auszahlung als
Buchung kommt: PayrollBooking(wage_type_code="UEBERSTUNDEN_AUSZAHLUNG",
value=22.08, year_month=2026-06-01) direkt anlegen (simuliert den
Task-2-Abschluss). tenant.settings: export_basis="soll"; features BEWUSST
OHNE payroll_bookings (Default AUS -> beweist den Bypass fuer System-Buchungen).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from zeiterfassung.core.security import create_access_token, hash_password
from zeiterfassung.models.absence import RequestType
from zeiterfassung.models.clock import EventSource, EventType, PunchEvent
from zeiterfassung.models.export import WageTypeMapping
from zeiterfassung.models.payroll_booking import PayrollBooking
from zeiterfassung.models.user import User
from zeiterfassung.policy.context import DayPart
from zeiterfassung.schemas.absence import AbsenceRequestCreate
from zeiterfassung.services.absence_service import create_request
from zeiterfassung.services.export_service import _build_datev_rows

# Juni 2026, Mo-Fr (kein Feiertag in HH): 1,2,3,4,5,8,9,10,11,12,15,16,17,18,
# 19,22,23,24,25,26,29,30 = 22 Arbeitstage. 29.06. (Mo) = Urlaub, nicht gestempelt.
# An 2 Tagen 9h (03. und 17.), an 1 Tag 7h (18.) gestempelt — Rest exakt 8h.
WORKDAYS = [1, 2, 3, 4, 5, 8, 9, 10, 11, 12, 15, 16, 17, 18, 19, 22, 23, 24, 25, 26, 30]
VACATION_DAY = date(2026, 6, 29)

NINE_HOUR_DAYS = {3, 17}
SEVEN_HOUR_DAYS = {18}

WAGE_TYPES = {
    "NORMALE_ARBEITSZEIT": 11,
    "URLAUB_STD": 131,
    "UEBERSTUNDEN_AUSZAHLUNG": 12,
    "MEHRARBEIT_0": 62,
    "MEHRARBEIT_ZUSCHLAG": 66,
    "MINUSSTUNDEN": 13,
}


def _utc(day: int, hhmm: str) -> datetime:
    """Lokale Berliner Zeit (Juni = CEST, UTC+2) -> UTC."""
    h, m = map(int, hhmm.split(":"))
    local_minutes = h * 60 + m - 120
    return datetime(2026, 6, day, local_minutes // 60, local_minutes % 60, tzinfo=UTC)


async def test_above_cap_keine_staffel_auszahlung_aus_abschluss(db, tenant) -> None:
    now = datetime.now(tz=UTC)
    employee = User(
        tenant_id=tenant.id,
        email="00042@example.com",
        hashed_password=hash_password("Passw0rd!"),
        first_name="Beta",
        last_name="Werker",
        employee_number="00042",
        contracted_hours_per_week=40.0,
        weekday_target_minutes=[480, 480, 480, 480, 480, 0, 0],
        employment_type="hourly",
        overtime_payout_mode="above_cap",
        overtime_cap_hours=20.0,
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    db.add(employee)
    await db.flush()

    # Export-Basis 'soll'; payroll_bookings-Feature BEWUSST NICHT gesetzt
    # (Default AUS) -> beweist den Bypass fuer System-Buchungen.
    tenant.settings = {
        **(tenant.settings or {}),
        "zuschlaege": {"export_basis": "soll", "mehrarbeit_frei_wochen_std": 1.5},
    }

    # Katalog OHNE Folgelohnarten fuer 131 (kein Urlaubsgeld-Zwilling hier).
    for nr in (11, 131, 12, 62, 66, 13):
        db.add(
            WageTypeMapping(
                tenant_id=tenant.id,
                internal_code=f"LA_{nr:04d}",
                datev_lohnart=nr,
                description=f"LA {nr}",
                is_active=True,
                unit="hours",
                folge_lohnart=None,
                created_at=now,
            )
        )

    # Stempelungen: 8h an normalen Tagen, 9h an 2 Tagen, 7h an 1 Tag.
    for day in WORKDAYS:
        if day in NINE_HOUR_DAYS:
            kommt, geht = "08:00", "17:30"
        elif day in SEVEN_HOUR_DAYS:
            kommt, geht = "08:00", "15:30"
        else:
            kommt, geht = "08:00", "16:30"
        for etype, t in (
            (EventType.PUNCH_IN, kommt),
            (EventType.BREAK_START, "12:00"),
            (EventType.BREAK_END, "12:30"),
            (EventType.PUNCH_OUT, geht),
        ):
            db.add(
                PunchEvent(
                    tenant_id=tenant.id,
                    employee_id=employee.id,
                    event_type=etype,
                    event_time=_utc(day, t),
                    source=EventSource.NFC_TERMINAL,
                    created_at=now,
                )
            )

    # Urlaub: Mo 29.06. (1 Tag = 8h Tagessoll).
    data = AbsenceRequestCreate.model_construct(
        request_type=RequestType.VACATION,
        date_from=VACATION_DAY,
        date_to=VACATION_DAY,
        day_part=DayPart.FULL,
        hours=None,
        sick_status=None,
        payload=None,
        notes=None,
    )
    await create_request(
        db,
        tenant.id,
        employee.id,
        data,
        "HH",
        auto_approve=True,
        approver_id=uuid.uuid4(),
    )

    # Auszahlung aus dem Monatsabschluss (simuliert Task 2): System-Buchung.
    db.add(
        PayrollBooking(
            tenant_id=tenant.id,
            employee_id=employee.id,
            year_month=date(2026, 6, 1),
            wage_type_code="UEBERSTUNDEN_AUSZAHLUNG",
            value=22.08,
            created_at=now,
            updated_at=now,
        )
    )

    await db.commit()

    rows = await _build_datev_rows(db, tenant.id, 2026, 6, [employee.id], WAGE_TYPES)
    by_lohnart: dict[int, float] = {}
    for r in rows:
        by_lohnart[r.lohnart] = round(by_lohnart.get(r.lohnart, 0.0) + r.wert, 2)

    assert by_lohnart == {11: 168.0, 131: 8.0, 12: 22.08}


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


async def _payroll_write_token(db: AsyncSession, tenant_id: uuid.UUID) -> str:
    from tests.conftest import ensure_login_account

    acc = await ensure_login_account(db, tenant_id)
    return create_access_token(
        user_id=str(acc.id),
        tenant_id=str(tenant_id),
        permissions=["payroll:write"],
        department_id=None,
        role_type="geschaeftsfuehrung",
        data_scope="global",
    )


async def test_manuelle_buchung_mit_reserviertem_system_code_wird_abgelehnt(
    db, client, tenant, employee
) -> None:
    """UEBERSTUNDEN_AUSZAHLUNG darf nur der Monatsabschluss buchen, nicht die manuelle API."""
    now = datetime.now(tz=UTC)
    db.add(
        WageTypeMapping(
            tenant_id=tenant.id,
            internal_code="UEBERSTUNDEN_AUSZAHLUNG",
            datev_lohnart=12,
            description="LA 12",
            is_active=True,
            unit="hours",
            folge_lohnart=None,
            created_at=now,
        )
    )
    await db.commit()

    r = await client.post(
        "/api/v1/payroll/bookings",
        json={
            "employee_id": str(employee.id),
            "wage_type_code": "UEBERSTUNDEN_AUSZAHLUNG",
            "year": 2026,
            "month": 6,
            "value": 5,
        },
        headers={"Authorization": f"Bearer {await _payroll_write_token(db, tenant.id)}"},
    )
    assert r.status_code == 422, r.text
    assert "Reservierter System-Code" in r.json()["detail"]
