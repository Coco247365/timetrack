"""Goldener LODAS-Test: identische Zeiten -> identische Export-Zeilen.

Nachgestellt: ein Mitarbeiter (Firma Alpha), Juni 2026 — Stempelzeiten vom echten
Altsystem-Stundenzettel, erwartete Werte aus der echten LODAS-Datei
2026-06_ALPHA_LODAS.txt. Export-Basis 'soll' (wie das Altsystem):

- 011 Zeitlohn = effektive Sollzeit (Bruttosoll 170:30 − Urlaub 46:45 = 123:45)
- Mehrarbeit woechentlich gestaffelt: erste 1:30/Woche ohne Zuschlag (062),
  Rest mit Zuschlag (066 + Folgelohnart 067)
- Nacht-Mehrarbeit separat (084), nicht in der Staffel
- Minus-Tage als Abzug (013)
- Urlaub tagesgenau (Fr = 5:30) auf 129 + Folgelohnart 150
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest_asyncio

from zeiterfassung.core.security import hash_password
from zeiterfassung.models.absence import RequestType
from zeiterfassung.models.clock import EventSource, EventType, PunchEvent
from zeiterfassung.models.export import WageTypeMapping
from zeiterfassung.models.user import User
from zeiterfassung.policy.context import DayPart
from zeiterfassung.schemas.absence import AbsenceRequestCreate
from zeiterfassung.services.absence_service import create_request
from zeiterfassung.services.export_service import _build_datev_rows

# Stundenzettel Juni 2026 (lokale Zeiten, Pause als Block 12:00–12:45 bzw. 12:00–12:30).
# (tag, kommt, geht, pause_min) — Ist ergibt exakt die "Zeit"-Spalte des Zettels.
SHEET = [
    (1, "06:31", "17:30", 45),  # 10:14
    (2, "07:00", "18:04", 45),  # 10:19
    (3, "06:30", "16:00", 45),  # 8:45 (manuell nachgetragen)
    (4, "07:05", "16:05", 45),  # 8:15
    (5, "05:57", "13:46", 30),  # 7:19, davon 3 Min Nacht (vor 6 Uhr)
    (8, "07:05", "17:46", 45),  # 9:56
    (9, "06:33", "16:12", 45),  # 8:54
    (10, "06:50", "17:12", 45),  # 9:37
    (11, "06:48", "16:41", 45),  # 9:08
    (12, "06:44", "15:58", 30),  # 8:44
    (15, "06:46", "17:14", 45),  # 9:43
    (16, "06:52", "17:05", 45),  # 9:28
    (17, "06:48", "16:15", 45),  # 8:42
    (18, "06:58", "15:03", 45),  # 7:20 -> −0:55
    (19, "07:00", "15:20", 30),  # 7:50
    (22, "06:50", "16:01", 45),  # 8:26
]

# Erwartung = echte LODAS-Zeilen fuer PNr 41001 (Werte in Dezimalstunden).
EXPECTED = {
    11: 123.75,  # Zeitlohn = effektive Sollzeit
    62: 4.68,  # Mehrarbeit 0% (Wochenkontingent 1:30)
    66: 15.10,  # Mehrarbeit mit Zuschlag (Grund)
    67: 15.10,  # ... Folgelohnart: Zuschlag 30%
    84: 0.05,  # Nacht-Mehrarbeit (05:57–06:00)
    13: 0.92,  # Minusstunden (18.06.: −0:55)
    129: 46.75,  # Urlaublohn (6 Tage tagesgenau, Fr = 5:30)
    150: 46.75,  # ... Folgelohnart: zus. Urlaubsgeld
}

WAGE_TYPES = {
    "NORMALE_ARBEITSZEIT": 11,
    "MEHRARBEIT_0": 62,
    "MEHRARBEIT_ZUSCHLAG": 66,
    "MINUSSTUNDEN": 13,
    "URLAUB_STD": 129,
    "NACHT_ARBEIT": 84,
}


def _utc(day: int, hhmm: str) -> datetime:
    """Lokale Berliner Zeit (Juni = CEST, UTC+2) -> UTC."""
    h, m = map(int, hhmm.split(":"))
    local_minutes = h * 60 + m - 120
    return datetime(2026, 6, day, local_minutes // 60, local_minutes % 60, tzinfo=UTC)


@pytest_asyncio.fixture
async def expo_employee(db, tenant) -> User:
    now = datetime.now(tz=UTC)
    u = User(
        tenant_id=tenant.id,
        email="41001@example.com",
        hashed_password=hash_password("Passw0rd!"),
        first_name="Alpha",
        last_name="Werker",
        employee_number="41001",
        contracted_hours_per_week=38.5,
        # Mo–Do 8:15 (495), Fr 5:30 (330) — wie im Altsystem-Zeitmodell 2000.
        weekday_target_minutes=[495, 495, 495, 495, 330, 0, 0],
        employment_type="hourly",
        overtime_payout_mode="all",
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    db.add(u)
    await db.flush()
    return u


class TestLodasGolden:
    async def test_juni_2026_reproduziert_echte_lodas_datei(
        self, db, tenant, expo_employee
    ) -> None:
        # Export-Basis 'soll' + Wochenkontingent 1:30 (Zone 38,5->40 Std).
        tenant.settings = {
            **(tenant.settings or {}),
            "zuschlaege": {"export_basis": "soll", "mehrarbeit_frei_wochen_std": 1.5},
        }
        # Katalog mit Folgelohnarten (066->067, 129->150).
        now = datetime.now(UTC)
        for nr, folge in (
            (11, None),
            (62, None),
            (66, 67),
            (67, None),
            (13, None),
            (129, 150),
            (150, None),
            (84, None),
        ):
            db.add(
                WageTypeMapping(
                    tenant_id=tenant.id,
                    internal_code=f"LA_{nr:04d}",
                    datev_lohnart=nr,
                    description=f"LA {nr}",
                    is_active=True,
                    unit="hours",
                    folge_lohnart=f"LA_{folge:04d}" if folge else None,
                    created_at=now,
                )
            )
        # Stempelungen (mit Mittagspause als BREAK).
        for day, kommt, geht, pause in SHEET:
            pause_start = "12:00"
            pause_end_min = 12 * 60 + pause
            pause_end = f"{pause_end_min // 60}:{pause_end_min % 60:02d}"
            for etype, t in (
                (EventType.PUNCH_IN, kommt),
                (EventType.BREAK_START, pause_start),
                (EventType.BREAK_END, pause_end),
                (EventType.PUNCH_OUT, geht),
            ):
                db.add(
                    PunchEvent(
                        tenant_id=tenant.id,
                        employee_id=expo_employee.id,
                        event_type=etype,
                        event_time=_utc(day, t),
                        source=EventSource.NFC_TERMINAL,
                        created_at=now,
                    )
                )
        # Urlaub: 23.–26.06. (Di–Fr) + 29.–30.06. (Mo–Di) = 6 Tage = 46:45.
        for frm, to in (
            (date(2026, 6, 23), date(2026, 6, 26)),
            (date(2026, 6, 29), date(2026, 6, 30)),
        ):
            data = AbsenceRequestCreate.model_construct(
                request_type=RequestType.VACATION,
                date_from=frm,
                date_to=to,
                day_part=DayPart.FULL,
                hours=None,
                sick_status=None,
                payload=None,
                notes=None,
            )
            await create_request(
                db,
                tenant.id,
                expo_employee.id,
                data,
                "HH",
                auto_approve=True,
                approver_id=uuid.uuid4(),
            )
        await db.commit()

        rows = await _build_datev_rows(db, tenant.id, 2026, 6, [expo_employee.id], WAGE_TYPES)
        by_lohnart: dict[int, float] = {}
        for r in rows:
            by_lohnart[r.lohnart] = round(by_lohnart.get(r.lohnart, 0.0) + r.wert, 2)

        assert by_lohnart == EXPECTED
