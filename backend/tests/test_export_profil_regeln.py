"""Profil-Regeln: fruehester_beginn (Kappung) und mehrarbeit_frei_wochen_std (Override).

Nachgestellt analog test_export_lodas_golden.py: ein Testtag mit fruehem
Arbeitsbeginn, ein Profil mit ``fruehester_beginn`` kappt die Bewertung auf
die konfigurierte Uhrzeit (wie das Altsystem "06:00 (05:19)"); ein
zweites Profil mit ``mehrarbeit_frei_wochen_std`` ueberschreibt das woechentliche
Freikontingent fuer Mehrarbeit ohne Zuschlag.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from zeiterfassung.core.security import create_access_token, hash_password
from zeiterfassung.models.clock import EventSource, EventType, PunchEvent
from zeiterfassung.models.export import WageTypeMapping
from zeiterfassung.models.user import User
from zeiterfassung.payroll.rules import WorkSegment, clip_segments_earliest_start
from zeiterfassung.services.export_service import _build_datev_rows

WAGE_TYPES = {
    "NORMALE_ARBEITSZEIT": 11,
    "MEHRARBEIT_0": 62,
    "MEHRARBEIT_ZUSCHLAG": 66,
    "MINUSSTUNDEN": 13,
    "NACHT_ARBEIT": 84,
}


def _utc(day: int, hhmm: str) -> datetime:
    """Lokale Berliner Zeit (CEST, UTC+2) -> UTC."""
    h, m = map(int, hhmm.split(":"))
    local_minutes = h * 60 + m - 120
    return datetime(2026, 6, day, local_minutes // 60, local_minutes % 60, tzinfo=UTC)


class TestClipSegmentsEarliestStart:
    """Reiner Unit-Test der Kappungs-Funktion (keine DB noetig)."""

    def test_start_vor_schwelle_wird_gekappt(self) -> None:
        # 05:20 lokal -> UTC 03:20 (CEST-Offset 120 Min).
        seg = WorkSegment(
            start=datetime(2026, 6, 1, 3, 20),
            end=datetime(2026, 6, 1, 12, 35),
        )
        [clipped] = clip_segments_earliest_start([seg], earliest_min=6 * 60, tz_offset_min=120)
        # Start soll lokal 06:00 sein -> UTC 04:00.
        assert clipped.start == datetime(2026, 6, 1, 4, 0)
        assert clipped.end == seg.end

    def test_segment_komplett_vor_schwelle_entfaellt(self) -> None:
        seg = WorkSegment(
            start=datetime(2026, 6, 1, 2, 0),  # 04:00 lokal
            end=datetime(2026, 6, 1, 3, 30),  # 05:30 lokal
        )
        result = clip_segments_earliest_start([seg], earliest_min=6 * 60, tz_offset_min=120)
        assert result == []

    def test_segment_nach_schwelle_bleibt_unveraendert(self) -> None:
        seg = WorkSegment(
            start=datetime(2026, 6, 1, 5, 0),  # 07:00 lokal
            end=datetime(2026, 6, 1, 12, 0),  # 14:00 lokal
        )
        [unchanged] = clip_segments_earliest_start([seg], earliest_min=6 * 60, tz_offset_min=120)
        assert unchanged.start == seg.start
        assert unchanged.end == seg.end


@pytest_asyncio.fixture
async def make_employee(db, tenant):
    async def _make(email: str, employee_number: str, **kwargs) -> User:
        now = datetime.now(tz=UTC)
        u = User(
            tenant_id=tenant.id,
            email=email,
            hashed_password=hash_password("Passw0rd!"),
            first_name="Test",
            last_name="Werker",
            employee_number=employee_number,
            contracted_hours_per_week=38.5,
            weekday_target_minutes=[495, 495, 495, 495, 330, 0, 0],
            employment_type="hourly",
            overtime_payout_mode="all",
            is_active=True,
            created_at=now,
            updated_at=now,
            **kwargs,
        )
        db.add(u)
        await db.flush()
        return u

    return _make


async def _punch(db, tenant, employee, day: int, kommt: str, geht: str, pause: int) -> None:
    now = datetime.now(UTC)
    events: list[tuple[str, str]] = [(EventType.PUNCH_IN, kommt)]
    if pause > 0:
        pause_start = "12:00"
        pause_end_min = 12 * 60 + pause
        pause_end = f"{pause_end_min // 60}:{pause_end_min % 60:02d}"
        events.append((EventType.BREAK_START, pause_start))
        events.append((EventType.BREAK_END, pause_end))
    events.append((EventType.PUNCH_OUT, geht))
    for etype, t in events:
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


async def _seed_catalog(db, tenant) -> None:
    now = datetime.now(UTC)
    for nr in (11, 62, 66, 84, 13):
        db.add(
            WageTypeMapping(
                tenant_id=tenant.id,
                internal_code=f"LA_{nr:04d}",
                datev_lohnart=nr,
                description=f"LA {nr}",
                is_active=True,
                unit="hours",
                created_at=now,
            )
        )


class TestExportFruehesterBeginn:
    async def test_kappung_entfernt_nachtstunden_und_rechnet_ab_schwelle(
        self, db, tenant, make_employee
    ) -> None:
        tenant.settings = {
            **(tenant.settings or {}),
            "zuschlaege": {"export_basis": "soll", "mehrarbeit_frei_wochen_std": 1.5},
            "wage_profiles": [
                {"id": "fruehschicht", "name": "Fruehschicht", "fruehester_beginn": "06:00"},
            ],
        }
        await _seed_catalog(db, tenant)
        employee = await make_employee("fruehester@test.de", "F001", wage_profile_id="fruehschicht")
        # Alle Werktage im Juni 2026 exakt auf Tagessoll punchen (Ist = Soll,
        # Differenz 0), damit nur der 01.06. das Testergebnis beeinflusst.
        weekday_target = {0: 495, 1: 495, 2: 495, 3: 495, 4: 330}  # Mo..Fr
        for day in range(1, 31):
            wd = date(2026, 6, day).weekday()
            if wd not in weekday_target:
                continue
            if day == 1:
                continue  # Testtag separat unten
            target = weekday_target[wd]
            geht_min = 6 * 60 + target
            geht = f"{geht_min // 60}:{geht_min % 60:02d}"
            await _punch(db, tenant, employee, day, "06:00", geht, 0)
        # 01.06. (Montag): 05:20-14:35, 45 Min Pause -> ohne Kappung Ist = 8:30,
        # davon Nacht 0:40 (05:20-06:00). Mit Kappung ab 06:00: Ist = 7:50.
        await _punch(db, tenant, employee, 1, "05:20", "14:35", 45)
        await db.commit()

        rows = await _build_datev_rows(db, tenant.id, 2026, 6, [employee.id], WAGE_TYPES)
        by_lohnart: dict[int, float] = {}
        for r in rows:
            by_lohnart[r.lohnart] = round(by_lohnart.get(r.lohnart, 0.0) + r.wert, 2)

        assert 84 not in by_lohnart  # keine NACHT_ARBEIT-Zeile
        # Tagessoll Mo (01.06.2026 ist ein Montag) = 8:15 = 495 Min.
        # Ist ab 06:00 gerechnet: 06:00-14:35 minus 45 Min Pause = 7:50 = 470 Min.
        # Differenz = 470 - 495 = -25 Min -> Minusstunden, kein Mehrarbeit-Zuschlag.
        assert 66 not in by_lohnart
        assert by_lohnart.get(13) == round(25 / 60, 2)


class TestExportMehrarbeitFreiOverride:
    async def test_profil_override_entfernt_freikontingent(self, db, tenant, make_employee) -> None:
        tenant.settings = {
            **(tenant.settings or {}),
            "zuschlaege": {"export_basis": "soll", "mehrarbeit_frei_wochen_std": 1.5},
            "wage_profiles": [
                {"id": "keinfrei", "name": "Kein Freikontingent", "mehrarbeit_frei_wochen_std": 0},
            ],
        }
        await _seed_catalog(db, tenant)
        # Mitarbeiter A: Profil-Override auf 0 -> alles Mehrarbeit mit Zuschlag.
        emp_override = await make_employee("override@test.de", "O001", wage_profile_id="keinfrei")
        # Mitarbeiter B: kein Profil -> Mandanten-Wert 1,5 Std bleibt.
        emp_default = await make_employee("default@test.de", "D001")

        # Montag 01.06.2026, Tagessoll 8:15 (495 Min); beide arbeiten 2h mehr -> 10:15 (615 Min).
        await _punch(db, tenant, emp_override, 1, "06:00", "16:15", 0)
        await _punch(db, tenant, emp_default, 1, "06:00", "16:15", 0)
        await db.commit()

        rows = await _build_datev_rows(
            db, tenant.id, 2026, 6, [emp_override.id, emp_default.id], WAGE_TYPES
        )
        by_emp: dict[str, dict[int, float]] = {}
        for r in rows:
            by_emp.setdefault(r.personalnummer, {})
            by_emp[r.personalnummer][r.lohnart] = round(
                by_emp[r.personalnummer].get(r.lohnart, 0.0) + r.wert, 2
            )

        # Override: komplette Mehrarbeit (2h) mit Zuschlag, keine MEHRARBEIT_0-Zeile.
        assert 62 not in by_emp["O001"]
        assert by_emp["O001"].get(66) == 2.0

        # Standard (Mandanten-Wert 1,5 Std): 1,5 Std frei, Rest (0,5 Std) mit Zuschlag.
        assert by_emp["D001"].get(62) == 1.5
        assert by_emp["D001"].get(66) == 0.5


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


async def _admin_headers(db: AsyncSession, tenant_id: uuid.UUID) -> dict[str, str]:
    from tests.conftest import ensure_login_account

    acc = await ensure_login_account(db, tenant_id)
    tok = create_access_token(
        user_id=str(acc.id),
        tenant_id=str(tenant_id),
        permissions=["admin:settings"],
        department_id=None,
        role_type="geschaeftsfuehrung",
        data_scope="global",
    )
    return {"Authorization": f"Bearer {tok}"}


class TestWageProfileRegelnApiValidierung:
    async def test_ungueltiger_fruehester_beginn_gibt_422(self, db, client, tenant) -> None:
        r = await client.patch(
            "/api/v1/admin/wage-profiles",
            json={"name": "Fruehschicht", "mapping": {}, "fruehester_beginn": "25:99"},
            headers=await _admin_headers(db, tenant.id),
        )
        assert r.status_code == 422, r.text

    async def test_regel_auf_standard_profil_gibt_422(self, db, client, tenant) -> None:
        r = await client.patch(
            "/api/v1/admin/wage-profiles",
            json={
                "id": "standard",
                "name": "Standard",
                "mapping": {},
                "fruehester_beginn": "06:00",
            },
            headers=await _admin_headers(db, tenant.id),
        )
        assert r.status_code == 422, r.text
        assert "Standard-Profil" in r.json()["detail"]

    async def test_mehrarbeit_frei_negativ_gibt_422(self, db, client, tenant) -> None:
        r = await client.patch(
            "/api/v1/admin/wage-profiles",
            json={
                "name": "Test",
                "mapping": {},
                "mehrarbeit_frei_wochen_std": -1,
            },
            headers=await _admin_headers(db, tenant.id),
        )
        assert r.status_code == 422, r.text

    async def test_gueltige_regeln_werden_gespeichert_und_geliefert(
        self, db, client, tenant
    ) -> None:
        r = await client.patch(
            "/api/v1/admin/wage-profiles",
            json={
                "id": "fruehschicht",
                "name": "Fruehschicht",
                "mapping": {},
                "fruehester_beginn": "06:00",
                "mehrarbeit_frei_wochen_std": 0,
            },
            headers=await _admin_headers(db, tenant.id),
        )
        assert r.status_code == 200, r.text

        r2 = await client.get(
            "/api/v1/admin/wage-profiles", headers=await _admin_headers(db, tenant.id)
        )
        assert r2.status_code == 200, r2.text
        profile = next(p for p in r2.json()["profiles"] if p["id"] == "fruehschicht")
        assert profile["fruehester_beginn"] == "06:00"
        assert profile["mehrarbeit_frei_wochen_std"] == 0


class TestStandardProfilName:
    async def test_standard_profil_name_wird_gespeichert_und_geliefert(
        self, db, client, tenant
    ) -> None:
        await _seed_catalog(db, tenant)
        await db.commit()
        r = await client.patch(
            "/api/v1/admin/wage-profiles",
            json={
                "id": "standard",
                "name": "Basis-Abrechnung",
                "mapping": {"NORMALE_ARBEITSZEIT": "LA_0011"},
            },
            headers=await _admin_headers(db, tenant.id),
        )
        assert r.status_code == 200, r.text

        r2 = await client.get(
            "/api/v1/admin/wage-profiles", headers=await _admin_headers(db, tenant.id)
        )
        assert r2.status_code == 200, r2.text
        standard = next(p for p in r2.json()["profiles"] if p["id"] == "standard")
        assert standard["name"] == "Basis-Abrechnung"
        assert standard["mapping"] == {"NORMALE_ARBEITSZEIT": "LA_0011"}

    async def test_standard_profil_regeln_bleiben_gesperrt_trotz_namensaenderung(
        self, db, client, tenant
    ) -> None:
        r = await client.patch(
            "/api/v1/admin/wage-profiles",
            json={
                "id": "standard",
                "name": "Umbenannt",
                "mapping": {},
                "mehrarbeit_frei_wochen_std": 2,
            },
            headers=await _admin_headers(db, tenant.id),
        )
        assert r.status_code == 422, r.text
        assert "Standard-Profil" in r.json()["detail"]
