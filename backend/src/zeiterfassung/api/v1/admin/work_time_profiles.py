"""Admin-CRUD fuer Arbeitszeit-Profile (Spec 2026-08-14, Baustein A).

Mounted unter ``/admin`` (Prefix kommt vom Aggregator ``api/v1/admin/__init__``)
— Pfade also ``/api/v1/admin/work-time-profiles``.

Rechte wie bei den Arbeitszeitmodellen (``settings.py``): Schreiben braucht
``admin:settings``; Lesen duerfen auch ``users:read_all``/``users:write``,
weil das Mitarbeiter-Stammdaten-Formular die Liste braucht.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status
from pydantic import ValidationError
from sqlalchemy import func, select, update

from zeiterfassung.api.deps import AuthUser, DBSession
from zeiterfassung.db.scoping import get_scoped, tenant_scoped
from zeiterfassung.models.user import User
from zeiterfassung.models.work_time_profile import WorkTimeProfile
from zeiterfassung.policy.worktime import auto_contracted_hours_per_week
from zeiterfassung.schemas.work_time_profile import (
    WorkTimeProfileCreate,
    WorkTimeProfileResponse,
    WorkTimeProfileUpdate,
)

router = APIRouter()


def _require_read(admin: AuthUser) -> None:
    if not (
        admin.has_permission("admin:settings")
        or admin.has_permission("users:read_all")
        or admin.has_permission("users:write")
        or admin.has_permission("admin")
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions"
        )


def _require_write(admin: AuthUser) -> None:
    if not admin.has_permission("admin:settings"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="admin:settings erforderlich"
        )


async def _assigned_count(db: DBSession, tenant_id: uuid.UUID, profile_id: uuid.UUID) -> int:
    stmt = (
        select(func.count())
        .select_from(User)
        .where(User.tenant_id == tenant_id, User.work_time_profile_id == profile_id)
    )
    return int((await db.execute(stmt)).scalar_one())


def _response(profile: WorkTimeProfile, assigned_count: int) -> WorkTimeProfileResponse:
    out = WorkTimeProfileResponse.model_validate(profile)
    out.assigned_count = assigned_count
    # Auto-Profile: Vertragsstunden werden nicht gespeichert (Spalte NULL),
    # sondern aus den Wochentags-Zeiten berechnet (Nachtrag 14.08.).
    if str(profile.kind) == "auto":
        out.contracted_hours_per_week = auto_contracted_hours_per_week(profile.weekday_times)
    return out


async def _check_name_free(
    db: DBSession, tenant_id: uuid.UUID, name: str, exclude_id: uuid.UUID | None = None
) -> None:
    stmt = tenant_scoped(select(WorkTimeProfile.id), WorkTimeProfile, tenant_id).where(
        WorkTimeProfile.name == name
    )
    if exclude_id is not None:
        stmt = stmt.where(WorkTimeProfile.id != exclude_id)
    if (await db.execute(stmt)).first() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Es gibt bereits ein Arbeitszeit-Profil namens {name!r}.",
        )


@router.get("/work-time-profiles", response_model=list[WorkTimeProfileResponse])
async def list_work_time_profiles(db: DBSession, admin: AuthUser) -> list[WorkTimeProfileResponse]:
    """Alle Profile des Mandanten inkl. Anzahl zugewiesener Mitarbeiter."""
    _require_read(admin)
    stmt = tenant_scoped(select(WorkTimeProfile), WorkTimeProfile, admin.tenant_id).order_by(
        WorkTimeProfile.name
    )
    profiles = (await db.execute(stmt)).scalars().all()

    counts_stmt = (
        select(User.work_time_profile_id, func.count())
        .where(User.tenant_id == admin.tenant_id, User.work_time_profile_id.is_not(None))
        .group_by(User.work_time_profile_id)
    )
    counts = dict((await db.execute(counts_stmt)).all())
    return [_response(p, int(counts.get(p.id, 0))) for p in profiles]


@router.post(
    "/work-time-profiles",
    response_model=WorkTimeProfileResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_work_time_profile(
    body: WorkTimeProfileCreate, db: DBSession, admin: AuthUser
) -> WorkTimeProfileResponse:
    """Neues Arbeitszeit-Profil anlegen (admin:settings)."""
    _require_write(admin)
    await _check_name_free(db, admin.tenant_id, body.name)
    profile = WorkTimeProfile(
        tenant_id=admin.tenant_id,
        name=body.name,
        kind=body.kind,
        is_active=body.is_active,
        clock_start=body.clock_start,
        clock_end=body.clock_end,
        weekday_times=(
            {k: (v.model_dump() if v is not None else None) for k, v in body.weekday_times.items()}
            if body.weekday_times is not None
            else None
        ),
        contracted_hours_per_week=body.contracted_hours_per_week,
        weekday_target_minutes=body.weekday_target_minutes,
    )
    db.add(profile)
    await db.commit()
    # updated_at/created_at sind server-generiert (func.now) — nach dem Commit
    # nachladen, sonst triggert die Response-Serialisierung Lazy-IO.
    await db.refresh(profile)
    return _response(profile, 0)


@router.patch("/work-time-profiles/{profile_id}", response_model=WorkTimeProfileResponse)
async def update_work_time_profile(
    profile_id: uuid.UUID, body: WorkTimeProfileUpdate, db: DBSession, admin: AuthUser
) -> WorkTimeProfileResponse:
    """Profil aendern (admin:settings).

    Die uebermittelten Felder werden mit dem Bestand gemerged und die
    Kombination erneut komplett validiert (Typ-Regeln, Zeitfenster).
    Ein ``kind``-Wechsel ist nur ohne zugewiesene Mitarbeiter erlaubt (409) —
    sonst wuerde die Typ-Bindung der bestehenden Zuweisungen brechen.
    """
    _require_write(admin)
    profile = await get_scoped(db, WorkTimeProfile, admin.tenant_id, profile_id)
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Arbeitszeit-Profil nicht gefunden"
        )

    update_data = body.model_dump(exclude_unset=True)
    assigned = await _assigned_count(db, admin.tenant_id, profile.id)
    if "kind" in update_data and update_data["kind"] != profile.kind and assigned > 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Der Profil-Typ kann nicht geändert werden, solange Mitarbeiter "
                "zugewiesen sind — erst die Zuweisungen entfernen."
            ),
        )
    if "name" in update_data:
        await _check_name_free(db, admin.tenant_id, update_data["name"], exclude_id=profile.id)

    merged = {
        "name": profile.name,
        "kind": str(profile.kind),
        "is_active": profile.is_active,
        "clock_start": profile.clock_start,
        "clock_end": profile.clock_end,
        "weekday_times": profile.weekday_times,
        "contracted_hours_per_week": profile.contracted_hours_per_week,
        "weekday_target_minutes": profile.weekday_target_minutes,
        **update_data,
    }
    try:
        # bestand_ohne_stunden_ok: NFC-Bestandsprofile (beide Stunden-Felder
        # NULL) duerfen weiter umbenannt/geaendert werden, ohne dass die
        # Neuanlage-Pflicht aus dem Nachtrag 14.08. zuschlaegt.
        validated = WorkTimeProfileCreate.model_validate(
            merged, context={"bestand_ohne_stunden_ok": True}
        )
    except ValidationError as exc:
        # include_input/include_context=False: der Merge enthaelt Decimal-
        # Werte und die ctx-Eintraege rohe ValueError-Objekte — beides kennt
        # der JSON-Encoder der Fehlerantwort nicht.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.errors(include_url=False, include_input=False, include_context=False),
        ) from exc

    profile.name = validated.name
    profile.kind = validated.kind
    profile.is_active = validated.is_active
    profile.clock_start = validated.clock_start
    profile.clock_end = validated.clock_end
    profile.weekday_times = (
        {k: (v.model_dump() if v is not None else None) for k, v in validated.weekday_times.items()}
        if validated.weekday_times is not None
        else None
    )
    profile.contracted_hours_per_week = validated.contracted_hours_per_week
    profile.weekday_target_minutes = validated.weekday_target_minutes
    await db.commit()
    # updated_at wird per onupdate=func.now() serverseitig neu gesetzt und ist
    # nach dem Commit abgelaufen — nachladen statt Lazy-IO in der Response.
    await db.refresh(profile)
    return _response(profile, assigned)


@router.delete("/work-time-profiles/{profile_id}", response_model=dict)
async def delete_work_time_profile(profile_id: uuid.UUID, db: DBSession, admin: AuthUser) -> dict:
    """Profil loeschen (admin:settings) — Zuweisungen werden geloest.

    Wie bei den Abrechnungsprofilen: zugewiesene Mitarbeiter verlieren das
    Profil (FK steht auf SET NULL) und fallen auf das Standardverhalten ihres
    Erfassungsmodus zurueck; die Antwort nennt, wie viele das waren, damit die
    UI es in der Bestaetigung anzeigen kann.
    """
    _require_write(admin)
    profile = await get_scoped(db, WorkTimeProfile, admin.tenant_id, profile_id)
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Arbeitszeit-Profil nicht gefunden"
        )
    detached = await _assigned_count(db, admin.tenant_id, profile.id)
    # Zuweisungen explizit loesen (nicht nur via FK SET NULL): deterministisch
    # auch dort, wo Fremdschluessel nicht erzwungen werden (SQLite-Tests).
    await db.execute(
        update(User)
        .where(User.tenant_id == admin.tenant_id, User.work_time_profile_id == profile.id)
        .values(work_time_profile_id=None)
    )
    await db.delete(profile)
    await db.commit()
    return {"ok": True, "detached_count": detached}
