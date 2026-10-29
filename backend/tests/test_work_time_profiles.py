"""API-Tests fuer Arbeitszeit-Profile (Baustein A: Modell, CRUD, Typ-Bindung).

Spec: docs/superpowers/specs/2026-08-14-arbeitszeit-profile-design.md.
Kappung/Auto-Fuellung sind spaetere Bausteine — hier nur Datenmodell,
CRUD-Endpoints unter /admin/work-time-profiles und die Typ-Bindung an
``users.time_capture`` beim PATCH /users/{id}.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from zeiterfassung.core.security import create_access_token

BASE = "/api/v1/admin/work-time-profiles"


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
    db: AsyncSession, tenant_id: uuid.UUID, permissions: list[str]
) -> dict[str, str]:
    from tests.conftest import ensure_login_account

    acc = await ensure_login_account(db, tenant_id)
    # Commit: eine (erwartete) 4xx-Antwort rollt die geteilte In-Memory-
    # SQLite-Verbindung zurueck und wuerde den nur geflushten Account wieder
    # wegwerfen — Folge-Requests bekaemen dann 401.
    await db.commit()
    tok = create_access_token(
        user_id=str(acc.id),
        tenant_id=str(tenant_id),
        permissions=permissions,
        department_id=None,
        role_type="geschaeftsfuehrung",
        data_scope="global",
    )
    return {"Authorization": f"Bearer {tok}"}


async def _admin_headers(db: AsyncSession, tenant_id: uuid.UUID) -> dict[str, str]:
    return await _headers(db, tenant_id, ["admin:settings", "users:write", "users:read_all"])


# Mo–Fr je 480 min = 40 h — die Summe MUSS zu contracted_hours_per_week passen.
NFC_TAGESSOLL = {"0": 480, "1": 480, "2": 480, "3": 480, "4": 480}
NFC_BODY = {
    "name": "Frühschicht",
    "kind": "nfc",
    "clock_start": "06:00",
    "clock_end": "16:30",
    "contracted_hours_per_week": 40,
    "weekday_target_minutes": NFC_TAGESSOLL,
}
# Mo–Fr je 480 min Tages-Soll (07:00–15:45 mit 45 min Pause) → berechnete
# 40 h/Woche, passend zum employee-Fixture (40.0). Seit der betrieblichen
# Pausenregel (> 4 h → 45 min) verlangt das Schema bei Auto-Profilen mit mehr
# als 4 h Arbeitszeit ein Pausenfenster von mindestens 45 min.
AUTO_BODY = {
    "name": "Büro",
    "kind": "auto",
    "weekday_times": {
        "0": {"start": "07:00", "end": "15:45", "break_start": "12:00", "break_end": "12:45"},
        "1": {"start": "07:00", "end": "15:45", "break_start": "12:00", "break_end": "12:45"},
        "2": {"start": "07:00", "end": "15:45", "break_start": "12:00", "break_end": "12:45"},
        "3": {"start": "07:00", "end": "15:45", "break_start": "12:00", "break_end": "12:45"},
        "4": {"start": "07:00", "end": "15:45", "break_start": "12:00", "break_end": "12:45"},
        "6": None,
    },
}


async def _create(client, headers, body) -> dict:
    r = await client.post(BASE, json=body, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


class TestCrud:
    async def test_create_nfc_profil(self, db, client, tenant) -> None:
        headers = await _admin_headers(db, tenant.id)
        body = await _create(client, headers, NFC_BODY)
        assert body["name"] == "Frühschicht"
        assert body["kind"] == "nfc"
        assert body["clock_start"] == "06:00"
        assert body["clock_end"] == "16:30"
        assert body["weekday_times"] is None
        assert body["is_active"] is True
        assert body["assigned_count"] == 0
        assert body["id"]

    async def test_create_auto_profil(self, db, client, tenant) -> None:
        headers = await _admin_headers(db, tenant.id)
        body = await _create(client, headers, AUTO_BODY)
        assert body["kind"] == "auto"
        assert body["clock_start"] is None
        assert body["clock_end"] is None
        assert body["weekday_times"]["0"]["break_start"] == "12:00"
        assert body["weekday_times"]["6"] is None

    async def test_nfc_nachtschicht_end_vor_start_erlaubt(self, db, client, tenant) -> None:
        headers = await _admin_headers(db, tenant.id)
        body = await _create(
            client,
            headers,
            {
                "name": "Nachtschicht",
                "kind": "nfc",
                "clock_start": "22:00",
                "clock_end": "06:15",
                "contracted_hours_per_week": 40,
                "weekday_target_minutes": NFC_TAGESSOLL,
            },
        )
        assert body["clock_start"] == "22:00"
        assert body["clock_end"] == "06:15"

    async def test_liste_mit_assigned_count(self, db, client, tenant, employee) -> None:
        headers = await _admin_headers(db, tenant.id)
        nfc = await _create(client, headers, NFC_BODY)
        await _create(client, headers, AUTO_BODY)

        r = await client.patch(
            f"/api/v1/users/{employee.id}",
            json={"work_time_profile_id": nfc["id"]},
            headers=headers,
        )
        assert r.status_code == 200, r.text

        r = await client.get(BASE, headers=headers)
        assert r.status_code == 200, r.text
        rows = {p["name"]: p for p in r.json()}
        assert rows["Frühschicht"]["assigned_count"] == 1
        assert rows["Büro"]["assigned_count"] == 0

    async def test_patch_profil(self, db, client, tenant) -> None:
        headers = await _admin_headers(db, tenant.id)
        created = await _create(client, headers, NFC_BODY)
        r = await client.patch(
            f"{BASE}/{created['id']}",
            json={"name": "Frühschicht neu", "clock_start": "05:30", "is_active": False},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["name"] == "Frühschicht neu"
        assert body["clock_start"] == "05:30"
        assert body["clock_end"] == "16:30"
        assert body["is_active"] is False

    async def test_delete_unzugewiesen(self, db, client, tenant) -> None:
        headers = await _admin_headers(db, tenant.id)
        created = await _create(client, headers, NFC_BODY)
        r = await client.delete(f"{BASE}/{created['id']}", headers=headers)
        assert r.status_code == 200, r.text
        assert r.json()["detached_count"] == 0
        r = await client.get(BASE, headers=headers)
        assert r.json() == []

    async def test_delete_loest_zuweisungen(self, db, client, tenant, employee) -> None:
        # Wie bei den Abrechnungsprofilen: Löschen entfernt die Zuweisungen
        # (FK SET NULL), die Antwort nennt die Anzahl für die UI-Bestätigung.
        headers = await _admin_headers(db, tenant.id)
        created = await _create(client, headers, NFC_BODY)
        r = await client.patch(
            f"/api/v1/users/{employee.id}",
            json={"work_time_profile_id": created["id"]},
            headers=headers,
        )
        assert r.status_code == 200, r.text

        r = await client.delete(f"{BASE}/{created['id']}", headers=headers)
        assert r.status_code == 200, r.text
        assert r.json()["detached_count"] == 1

        db.expunge_all()
        from zeiterfassung.models.user import User

        emp = await db.get(User, employee.id)
        assert emp.work_time_profile_id is None

    async def test_doppelter_name_409(self, db, client, tenant) -> None:
        headers = await _admin_headers(db, tenant.id)
        await _create(client, headers, NFC_BODY)
        r = await client.post(BASE, json=NFC_BODY, headers=headers)
        assert r.status_code == 409, r.text

    async def test_patch_unbekannte_id_404(self, db, client, tenant) -> None:
        headers = await _admin_headers(db, tenant.id)
        r = await client.patch(f"{BASE}/{uuid.uuid4()}", json={"name": "x"}, headers=headers)
        assert r.status_code == 404, r.text

    async def test_kind_wechsel_mit_zuweisung_409(self, db, client, tenant, employee) -> None:
        headers = await _admin_headers(db, tenant.id)
        created = await _create(client, headers, NFC_BODY)
        r = await client.patch(
            f"/api/v1/users/{employee.id}",
            json={"work_time_profile_id": created["id"]},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        r = await client.patch(
            f"{BASE}/{created['id']}",
            json={"kind": "auto", "clock_start": None, "clock_end": None},
            headers=headers,
        )
        assert r.status_code == 409, r.text


class TestPermissions:
    async def test_post_ohne_admin_settings_403(self, db, client, tenant) -> None:
        headers = await _headers(db, tenant.id, ["users:read_all"])
        r = await client.post(BASE, json=NFC_BODY, headers=headers)
        assert r.status_code == 403, r.text

    async def test_get_mit_users_read_all_erlaubt(self, db, client, tenant) -> None:
        headers = await _headers(db, tenant.id, ["users:read_all"])
        r = await client.get(BASE, headers=headers)
        assert r.status_code == 200, r.text


class TestValidierung:
    async def test_kaputtes_hhmm_422(self, db, client, tenant) -> None:
        headers = await _admin_headers(db, tenant.id)
        for bad in ("25:00", "7:00", "07:60", "0700", "早い"):
            r = await client.post(
                BASE,
                json={"name": f"X {bad}", "kind": "nfc", "clock_start": bad},
                headers=headers,
            )
            assert r.status_code == 422, f"{bad}: {r.text}"

    async def test_auto_mit_clock_start_422(self, db, client, tenant) -> None:
        headers = await _admin_headers(db, tenant.id)
        r = await client.post(
            BASE,
            json={"name": "X", "kind": "auto", "clock_start": "07:00"},
            headers=headers,
        )
        assert r.status_code == 422, r.text

    async def test_nfc_mit_weekday_times_422(self, db, client, tenant) -> None:
        headers = await _admin_headers(db, tenant.id)
        r = await client.post(
            BASE,
            json={
                "name": "X",
                "kind": "nfc",
                "weekday_times": {"0": {"start": "07:00", "end": "15:00"}},
            },
            headers=headers,
        )
        assert r.status_code == 422, r.text

    async def test_auto_pausenregel_45min_ab_4h_422(self, db, client, tenant) -> None:
        """Betriebsregel: > 4 h Arbeitszeit im Tagesfenster → Pause ≥ 45 min Pflicht."""
        headers = await _admin_headers(db, tenant.id)
        cases = [
            # 8 h ohne Pausenfenster
            {"start": "07:00", "end": "15:00"},
            # 8,5 h mit nur 30 min Pause
            {"start": "07:00", "end": "15:30", "break_start": "12:00", "break_end": "12:30"},
        ]
        for day in cases:
            r = await client.post(
                BASE,
                json={"name": "X", "kind": "auto", "weekday_times": {"0": day}},
                headers=headers,
            )
            assert r.status_code == 422, f"{day}: {r.text}"
            assert "45" in r.text

    async def test_auto_pausenregel_4h_halbtag_ohne_pause_ok(self, db, client, tenant) -> None:
        """Exakt 4 h (Halbtags-Fenster) bleibt ohne Pausenfenster erlaubt."""
        headers = await _admin_headers(db, tenant.id)
        r = await client.post(
            BASE,
            json={
                "name": "Vormittage",
                "kind": "auto",
                "weekday_times": {"0": {"start": "07:30", "end": "11:30"}},
            },
            headers=headers,
        )
        assert r.status_code == 201, r.text

    async def test_auto_start_nicht_vor_end_422(self, db, client, tenant) -> None:
        headers = await _admin_headers(db, tenant.id)
        for start, end in (("15:30", "07:00"), ("07:00", "07:00")):
            day = {"start": start, "end": end}
            r = await client.post(
                BASE,
                json={"name": "X", "kind": "auto", "weekday_times": {"0": day}},
                headers=headers,
            )
            assert r.status_code == 422, f"{start}-{end}: {r.text}"

    async def test_break_ausserhalb_fenster_422(self, db, client, tenant) -> None:
        headers = await _admin_headers(db, tenant.id)
        cases = [
            # Pause vor Fensterbeginn
            {"start": "07:00", "end": "15:00", "break_start": "06:00", "break_end": "06:30"},
            # Pause ragt ueber Fensterende hinaus
            {"start": "07:00", "end": "15:00", "break_start": "14:45", "break_end": "15:15"},
            # break_start >= break_end
            {"start": "07:00", "end": "15:00", "break_start": "12:30", "break_end": "12:00"},
            # nur eine Seite gesetzt
            {"start": "07:00", "end": "15:00", "break_start": "12:00"},
        ]
        for day in cases:
            r = await client.post(
                BASE,
                json={"name": "X", "kind": "auto", "weekday_times": {"0": day}},
                headers=headers,
            )
            assert r.status_code == 422, f"{day}: {r.text}"

    async def test_weekday_key_ausserhalb_0_bis_6_422(self, db, client, tenant) -> None:
        headers = await _admin_headers(db, tenant.id)
        r = await client.post(
            BASE,
            json={
                "name": "X",
                "kind": "auto",
                "weekday_times": {"7": {"start": "07:00", "end": "15:00"}},
            },
            headers=headers,
        )
        assert r.status_code == 422, r.text

    async def test_nfc_start_gleich_end_422(self, db, client, tenant) -> None:
        headers = await _admin_headers(db, tenant.id)
        r = await client.post(
            BASE,
            json={"name": "X", "kind": "nfc", "clock_start": "08:00", "clock_end": "08:00"},
            headers=headers,
        )
        assert r.status_code == 422, r.text


class TestTypBindung:
    async def _set_capture(self, db, employee, mode: str) -> None:
        employee.time_capture = mode
        await db.commit()

    async def test_nfc_profil_an_nfc_mitarbeiter(self, db, client, tenant, employee) -> None:
        headers = await _admin_headers(db, tenant.id)
        created = await _create(client, headers, NFC_BODY)
        r = await client.patch(
            f"/api/v1/users/{employee.id}",
            json={"work_time_profile_id": created["id"]},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        assert r.json()["work_time_profile_id"] == created["id"]

    async def test_nfc_profil_an_web_mitarbeiter(self, db, client, tenant, employee) -> None:
        await self._set_capture(db, employee, "web")
        headers = await _admin_headers(db, tenant.id)
        created = await _create(client, headers, NFC_BODY)
        r = await client.patch(
            f"/api/v1/users/{employee.id}",
            json={"work_time_profile_id": created["id"]},
            headers=headers,
        )
        assert r.status_code == 200, r.text

    async def test_nfc_profil_an_auto_mitarbeiter_422(self, db, client, tenant, employee) -> None:
        await self._set_capture(db, employee, "auto")
        headers = await _admin_headers(db, tenant.id)
        created = await _create(client, headers, NFC_BODY)
        r = await client.patch(
            f"/api/v1/users/{employee.id}",
            json={"work_time_profile_id": created["id"]},
            headers=headers,
        )
        assert r.status_code == 422, r.text

    async def test_auto_profil_an_nfc_mitarbeiter_422(self, db, client, tenant, employee) -> None:
        headers = await _admin_headers(db, tenant.id)
        created = await _create(client, headers, AUTO_BODY)
        r = await client.patch(
            f"/api/v1/users/{employee.id}",
            json={"work_time_profile_id": created["id"]},
            headers=headers,
        )
        assert r.status_code == 422, r.text

    async def test_auto_profil_an_auto_mitarbeiter(self, db, client, tenant, employee) -> None:
        await self._set_capture(db, employee, "auto")
        headers = await _admin_headers(db, tenant.id)
        created = await _create(client, headers, AUTO_BODY)
        r = await client.patch(
            f"/api/v1/users/{employee.id}",
            json={"work_time_profile_id": created["id"]},
            headers=headers,
        )
        assert r.status_code == 200, r.text

    async def test_gleichzeitig_time_capture_und_profil(self, db, client, tenant, employee) -> None:
        """Beide Felder im selben PATCH: gegen das NEUE time_capture pruefen."""
        headers = await _admin_headers(db, tenant.id)
        created = await _create(client, headers, AUTO_BODY)
        r = await client.patch(
            f"/api/v1/users/{employee.id}",
            json={"work_time_profile_id": created["id"], "time_capture": "auto"},
            headers=headers,
        )
        assert r.status_code == 200, r.text

    async def test_moduswechsel_mit_inkompatiblem_profil_422(
        self, db, client, tenant, employee
    ) -> None:
        headers = await _admin_headers(db, tenant.id)
        created = await _create(client, headers, NFC_BODY)
        r = await client.patch(
            f"/api/v1/users/{employee.id}",
            json={"work_time_profile_id": created["id"]},
            headers=headers,
        )
        assert r.status_code == 200, r.text

        r = await client.patch(
            f"/api/v1/users/{employee.id}",
            json={"time_capture": "auto"},
            headers=headers,
        )
        assert r.status_code == 422, r.text
        assert "Arbeitszeit-Profil" in r.text

    async def test_moduswechsel_nfc_zu_web_mit_nfc_profil_ok(
        self, db, client, tenant, employee
    ) -> None:
        headers = await _admin_headers(db, tenant.id)
        created = await _create(client, headers, NFC_BODY)
        r = await client.patch(
            f"/api/v1/users/{employee.id}",
            json={"work_time_profile_id": created["id"]},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        r = await client.patch(
            f"/api/v1/users/{employee.id}",
            json={"time_capture": "web"},
            headers=headers,
        )
        assert r.status_code == 200, r.text

    async def test_fremdes_tenant_profil_422(self, db, client, tenant, employee) -> None:
        from datetime import UTC, datetime

        from zeiterfassung.models.tenant import Tenant
        from zeiterfassung.models.work_time_profile import WorkTimeProfile

        now = datetime.now(tz=UTC)
        other = Tenant(
            name="Fremd GmbH",
            timezone="Europe/Berlin",
            locale="de-DE",
            bundesland="HH",
            is_active=True,
            created_at=now,
            updated_at=now,
        )
        db.add(other)
        await db.flush()
        foreign = WorkTimeProfile(tenant_id=other.id, name="Fremd", kind="nfc", is_active=True)
        db.add(foreign)
        await db.commit()

        headers = await _admin_headers(db, tenant.id)
        r = await client.patch(
            f"/api/v1/users/{employee.id}",
            json={"work_time_profile_id": str(foreign.id)},
            headers=headers,
        )
        assert r.status_code == 422, r.text

    async def test_fremdes_tenant_profil_nicht_in_liste(self, db, client, tenant) -> None:
        from datetime import UTC, datetime

        from zeiterfassung.models.tenant import Tenant
        from zeiterfassung.models.work_time_profile import WorkTimeProfile

        now = datetime.now(tz=UTC)
        other = Tenant(
            name="Fremd GmbH",
            timezone="Europe/Berlin",
            locale="de-DE",
            bundesland="HH",
            is_active=True,
            created_at=now,
            updated_at=now,
        )
        db.add(other)
        await db.flush()
        db.add(WorkTimeProfile(tenant_id=other.id, name="Fremd", kind="nfc", is_active=True))
        await db.commit()

        headers = await _admin_headers(db, tenant.id)
        r = await client.get(BASE, headers=headers)
        assert r.status_code == 200, r.text
        assert r.json() == []


# ---------------------------------------------------------------------------
# Nachtrag 14.08.: Vertragsstunden + Tagessoll im Profil
# ---------------------------------------------------------------------------


async def _altes_nfc_profil(db, tenant, *, name: str = "Bestand"):
    """Bestandsprofil wie vor dem Nachtrag: beide Stunden-Felder NULL."""
    from zeiterfassung.models.work_time_profile import WorkTimeProfile, WorkTimeProfileKind

    profil = WorkTimeProfile(
        tenant_id=tenant.id,
        name=name,
        kind=WorkTimeProfileKind.NFC,
        is_active=True,
        clock_start="06:00",
        clock_end="16:30",
    )
    db.add(profil)
    await db.commit()
    return profil


class TestVertragsstundenValidierung:
    async def test_nfc_create_ohne_stunden_422(self, db, client, tenant) -> None:
        headers = await _admin_headers(db, tenant.id)
        body = {
            k: v
            for k, v in NFC_BODY.items()
            if k not in ("contracted_hours_per_week", "weekday_target_minutes")
        }
        r = await client.post(BASE, json=body, headers=headers)
        assert r.status_code == 422, r.text

    async def test_nfc_create_nur_ein_feld_422(self, db, client, tenant) -> None:
        headers = await _admin_headers(db, tenant.id)
        nur_stunden = {k: v for k, v in NFC_BODY.items() if k != "weekday_target_minutes"}
        nur_soll = {k: v for k, v in NFC_BODY.items() if k != "contracted_hours_per_week"}
        for body in (nur_stunden, nur_soll):
            r = await client.post(BASE, json=body, headers=headers)
            assert r.status_code == 422, r.text

    async def test_summe_passt_nicht_422_nennt_differenz(self, db, client, tenant) -> None:
        headers = await _admin_headers(db, tenant.id)
        # 4×480 + 450 = 2370 min = 39:30 h, es fehlen 0:30 auf 40 h.
        body = {**NFC_BODY, "weekday_target_minutes": {**NFC_TAGESSOLL, "4": 450}}
        r = await client.post(BASE, json=body, headers=headers)
        assert r.status_code == 422, r.text
        assert "39:30" in r.text
        assert "fehlen 0:30" in r.text

    async def test_summe_zu_hoch_422_nennt_differenz(self, db, client, tenant) -> None:
        headers = await _admin_headers(db, tenant.id)
        # 5×480 + 30 (Sa) = 2430 min → 0:30 zu viel.
        body = {**NFC_BODY, "weekday_target_minutes": {**NFC_TAGESSOLL, "5": 30}}
        r = await client.post(BASE, json=body, headers=headers)
        assert r.status_code == 422, r.text
        assert "0:30 zu viel" in r.text

    async def test_tagessoll_wertebereich_422(self, db, client, tenant) -> None:
        headers = await _admin_headers(db, tenant.id)
        for bad in ({"0": -1}, {"0": 1441}, {"7": 480}):
            r = await client.post(
                BASE,
                json={**NFC_BODY, "weekday_target_minutes": bad},
                headers=headers,
            )
            assert r.status_code == 422, f"{bad}: {r.text}"

    async def test_auto_mit_stunden_feldern_422(self, db, client, tenant) -> None:
        headers = await _admin_headers(db, tenant.id)
        for extra in (
            {"contracted_hours_per_week": 40},
            {"weekday_target_minutes": NFC_TAGESSOLL},
        ):
            r = await client.post(BASE, json={**AUTO_BODY, **extra}, headers=headers)
            assert r.status_code == 422, f"{extra}: {r.text}"

    async def test_nfc_response_liefert_stunden_und_tagessoll(self, db, client, tenant) -> None:
        headers = await _admin_headers(db, tenant.id)
        body = await _create(client, headers, NFC_BODY)
        assert Decimal(str(body["contracted_hours_per_week"])) == Decimal("40")
        assert body["weekday_target_minutes"] == NFC_TAGESSOLL

    async def test_auto_response_liefert_berechnete_stunden(self, db, client, tenant) -> None:
        # Mo–Fr je 480 min Tages-Soll (inkl. Pflichtpausen-Logik) → 40.00 h.
        headers = await _admin_headers(db, tenant.id)
        body = await _create(client, headers, AUTO_BODY)
        assert Decimal(str(body["contracted_hours_per_week"])) == Decimal("40")
        assert body["weekday_target_minutes"] is None

        r = await client.get(BASE, headers=headers)
        assert r.status_code == 200, r.text
        row = {p["name"]: p for p in r.json()}["Büro"]
        assert Decimal(str(row["contracted_hours_per_week"])) == Decimal("40")

    async def test_auto_ohne_weekday_times_stunden_none(self, db, client, tenant) -> None:
        headers = await _admin_headers(db, tenant.id)
        body = await _create(client, headers, {"name": "Frei", "kind": "auto"})
        assert body["contracted_hours_per_week"] is None

    async def test_bestandsprofil_umbenennen_bleibt_erlaubt(self, db, client, tenant) -> None:
        # Alt-Profil (beide Felder NULL) darf ohne Stunden-Nachtrag geändert werden.
        profil = await _altes_nfc_profil(db, tenant)
        headers = await _admin_headers(db, tenant.id)
        r = await client.patch(f"{BASE}/{profil.id}", json={"name": "Bestand neu"}, headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["name"] == "Bestand neu"
        assert body["contracted_hours_per_week"] is None
        assert body["weekday_target_minutes"] is None

    async def test_patch_nur_ein_stunden_feld_422(self, db, client, tenant) -> None:
        profil = await _altes_nfc_profil(db, tenant)
        headers = await _admin_headers(db, tenant.id)
        r = await client.patch(
            f"{BASE}/{profil.id}",
            json={"contracted_hours_per_week": 40},
            headers=headers,
        )
        assert r.status_code == 422, r.text

    async def test_patch_stunden_nachtragen_ok(self, db, client, tenant) -> None:
        profil = await _altes_nfc_profil(db, tenant)
        headers = await _admin_headers(db, tenant.id)
        r = await client.patch(
            f"{BASE}/{profil.id}",
            json={"contracted_hours_per_week": 40, "weekday_target_minutes": NFC_TAGESSOLL},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        assert Decimal(str(r.json()["contracted_hours_per_week"])) == Decimal("40")


class TestStundenBindung:
    async def _patch_user(self, client, headers, employee, payload):
        return await client.patch(f"/api/v1/users/{employee.id}", json=payload, headers=headers)

    async def test_nfc_zuweisung_stunden_match_200(self, db, client, tenant, employee) -> None:
        headers = await _admin_headers(db, tenant.id)
        created = await _create(client, headers, NFC_BODY)  # 40 h, Fixture hat 40.0
        r = await self._patch_user(
            client, headers, employee, {"work_time_profile_id": created["id"]}
        )
        assert r.status_code == 200, r.text

    async def test_nfc_zuweisung_stunden_mismatch_422(self, db, client, tenant, employee) -> None:
        employee.contracted_hours_per_week = 30.0
        await db.commit()
        headers = await _admin_headers(db, tenant.id)
        created = await _create(client, headers, NFC_BODY)
        r = await self._patch_user(
            client, headers, employee, {"work_time_profile_id": created["id"]}
        )
        assert r.status_code == 422, r.text
        assert "ausgelegt" in r.text
        assert "40" in r.text and "30" in r.text

    async def test_auto_zuweisung_stunden_mismatch_422(self, db, client, tenant, employee) -> None:
        employee.time_capture = "auto"
        employee.contracted_hours_per_week = 38.5
        await db.commit()
        headers = await _admin_headers(db, tenant.id)
        created = await _create(client, headers, AUTO_BODY)  # berechnet 40 h
        r = await self._patch_user(
            client, headers, employee, {"work_time_profile_id": created["id"]}
        )
        assert r.status_code == 422, r.text
        assert "ausgelegt" in r.text

    async def test_auto_zuweisung_stunden_match_200(self, db, client, tenant, employee) -> None:
        employee.time_capture = "auto"
        await db.commit()
        headers = await _admin_headers(db, tenant.id)
        created = await _create(client, headers, AUTO_BODY)  # berechnet 40 h == Fixture
        r = await self._patch_user(
            client, headers, employee, {"work_time_profile_id": created["id"]}
        )
        assert r.status_code == 200, r.text

    async def test_auto_ohne_weekday_times_bindet_nicht(self, db, client, tenant, employee) -> None:
        employee.time_capture = "auto"
        employee.contracted_hours_per_week = 12.0
        await db.commit()
        headers = await _admin_headers(db, tenant.id)
        created = await _create(client, headers, {"name": "Frei", "kind": "auto"})
        r = await self._patch_user(
            client, headers, employee, {"work_time_profile_id": created["id"]}
        )
        assert r.status_code == 200, r.text

    async def test_altes_profil_ohne_stunden_bindet_nicht(
        self, db, client, tenant, employee
    ) -> None:
        employee.contracted_hours_per_week = 25.0
        await db.commit()
        profil = await _altes_nfc_profil(db, tenant)
        headers = await _admin_headers(db, tenant.id)
        r = await self._patch_user(
            client, headers, employee, {"work_time_profile_id": str(profil.id)}
        )
        assert r.status_code == 200, r.text

    async def test_user_stundenaenderung_mit_gebundenem_profil_422(
        self, db, client, tenant, employee
    ) -> None:
        headers = await _admin_headers(db, tenant.id)
        created = await _create(client, headers, NFC_BODY)
        r = await self._patch_user(
            client, headers, employee, {"work_time_profile_id": created["id"]}
        )
        assert r.status_code == 200, r.text

        r = await self._patch_user(client, headers, employee, {"contracted_hours_per_week": 35})
        assert r.status_code == 422, r.text
        assert "entfernen" in r.text

        # Gleicher Wert bleibt natürlich erlaubt.
        r = await self._patch_user(client, headers, employee, {"contracted_hours_per_week": 40})
        assert r.status_code == 200, r.text


class TestNfcSollHelper:
    """DB-freie Policy-Helper-Tests für nfc_target_minutes (Muster: test_auto_profil)."""

    def _profil(self, *, kind="nfc", is_active=True, hours=Decimal("40"), soll=None):
        class P:
            pass

        p = P()
        p.kind = kind
        p.is_active = is_active
        p.contracted_hours_per_week = hours
        p.weekday_target_minutes = (
            soll if soll is not None else {"0": 480, "1": 480, "2": 480, "3": 480, "4": 480}
        )
        return p

    def test_greift_nicht_ohne_profil_inaktiv_auto_oder_ohne_stunden(self) -> None:
        from zeiterfassung.policy import worktime

        assert worktime.nfc_target_minutes(None, 0) is None
        assert worktime.nfc_target_minutes(self._profil(is_active=False), 0) is None
        assert worktime.nfc_target_minutes(self._profil(kind="auto"), 0) is None
        assert worktime.nfc_target_minutes(self._profil(hours=None), 0) is None

    def test_tagessoll_und_fehlender_tag_ist_0(self) -> None:
        from zeiterfassung.policy import worktime

        p = self._profil()
        assert worktime.nfc_target_minutes(p, 0) == 480
        assert worktime.nfc_target_minutes(p, 5) == 0  # Sa fehlt im Dict → 0


class TestSollKette:
    """Konto + recompute nehmen das Tages-Soll aus dem stundengebundenen NFC-Profil.

    Fixtage wie test_auto_profil: Mo 6.7.2026 ist Werktag (kein Feiertag HH),
    Europe/Berlin = UTC+2.
    """

    async def _nfc_stunden_profil(self, db, tenant, employee, *, is_active=True):
        from zeiterfassung.models.work_time_profile import WorkTimeProfile, WorkTimeProfileKind

        profil = WorkTimeProfile(
            tenant_id=tenant.id,
            name=f"NFC {uuid.uuid4().hex[:6]}",
            kind=WorkTimeProfileKind.NFC,
            is_active=is_active,
            contracted_hours_per_week=40,
            weekday_target_minutes={"0": 480, "1": 480, "2": 480, "3": 480, "4": 480},
        )
        db.add(profil)
        await db.flush()
        employee.work_time_profile_id = profil.id
        # Abweichender User-Plan (Mo–Fr 300): OHNE Profil-Soll käme überall 300.
        employee.weekday_target_minutes = [300, 300, 300, 300, 300, 0, 0]
        await db.flush()
        return profil

    async def test_daily_rows_nutzen_profil_soll(self, db, tenant, employee) -> None:
        from zeiterfassung.services import overtime_service as ot

        await self._nfc_stunden_profil(db, tenant, employee)
        rows = await ot._daily_rows_for_month(
            db, tenant.id, employee, 2026, 7, today=date(2026, 7, 7)
        )
        by_date = {r["work_date"]: r for r in rows}
        assert by_date[date(2026, 7, 6)]["target_minutes"] == 480

    async def test_daily_rows_inaktives_profil_faellt_zurueck(self, db, tenant, employee) -> None:
        from zeiterfassung.services import overtime_service as ot

        await self._nfc_stunden_profil(db, tenant, employee, is_active=False)
        rows = await ot._daily_rows_for_month(
            db, tenant.id, employee, 2026, 7, today=date(2026, 7, 7)
        )
        by_date = {r["work_date"]: r for r in rows}
        assert by_date[date(2026, 7, 6)]["target_minutes"] == 300

    async def test_recompute_overtime_nutzt_profil_soll(self, db, tenant, employee) -> None:
        from zeiterfassung.models.clock import EventSource, EventType
        from zeiterfassung.services import clock_service

        await self._nfc_stunden_profil(db, tenant, employee)
        # Mo 6.7.: 06:00–15:30 lokal (04:00–13:30 UTC) ohne Pausen-Stempel →
        # Anwesenheit 570, Pflichtpause 30, net 540. Profil-Soll Mo = 480 →
        # Überstunden 60 (mit dem 300er-User-Plan wären es 240).
        for etype, hh, mm in ((EventType.PUNCH_IN, 4, 0), (EventType.PUNCH_OUT, 13, 30)):
            await clock_service.record_punch(
                db=db,
                tenant_id=tenant.id,
                employee_id=employee.id,
                event_type=etype,
                source=EventSource.NFC_TERMINAL,
                event_time=datetime(2026, 7, 6, hh, mm, tzinfo=UTC),
            )
        ts = await clock_service.recompute_daily_timesheet(
            db, tenant.id, employee.id, date(2026, 7, 6)
        )
        assert ts.net_work_minutes == 540
        assert ts.overtime_minutes == 60
