"""DATEV payroll export API endpoints."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from fastapi import APIRouter, HTTPException, Response, status

from zeiterfassung.api.deps import AuthUser, DBSession
from zeiterfassung.core.errors import ErrorCode, api_error
from zeiterfassung.models.export import ExportStatus, PayrollExport
from zeiterfassung.schemas.export import (
    DatevExportRequest,
    DatevPreviewResponse,
    PayrollExportResponse,
)
from zeiterfassung.services import export_service

router = APIRouter()


def _require_datev_permission(current_user: AuthUser) -> None:  # type: ignore[name-defined]
    """Raise 403 unless the caller holds ``export:datev`` or legacy ``admin``."""
    if not current_user.has_permission(
        "export:datev"
    ) and not current_user.has_permission_namespace("admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="export:datev permission required",
        )


@router.post("/datev/preview", response_model=DatevPreviewResponse)
async def preview_datev(
    body: DatevExportRequest,
    db: DBSession,
    current_user: AuthUser,
) -> DatevPreviewResponse:
    """Preview the DATEV export rows without persisting anything.

    Requires ``export:datev`` permission (GF, HR, Buchhaltung).
    """
    _require_datev_permission(current_user)
    return await export_service.preview_export(db, current_user.tenant_id, body)


@router.post(
    "/datev/generate",
    response_model=PayrollExportResponse,
    status_code=status.HTTP_201_CREATED,
)
async def generate_datev(
    body: DatevExportRequest,
    db: DBSession,
    current_user: AuthUser,
) -> PayrollExportResponse:
    """Generate and persist a DATEV CSV export.

    Requires ``export:datev`` permission (GF, HR, Buchhaltung).
    """
    _require_datev_permission(current_user)
    try:
        export, _csv_bytes = await export_service.generate_export(
            db, current_user.tenant_id, body, current_user.user_id
        )
    except ValueError as exc:
        raise api_error(status.HTTP_400_BAD_REQUEST, exc, code=ErrorCode.EXPORT_VALIDATION) from exc
    await db.commit()
    return PayrollExportResponse.model_validate(export)


@router.get("/datev/history", response_model=list[PayrollExportResponse])
async def export_history(
    db: DBSession,
    current_user: AuthUser,
    skip: int = 0,
    # Default-Limit: Liste ist gekappt (neueste zuerst) — Konsument muss
    # paginieren oder server-seitig filtern (Bug-Klasse #163/#165).
    limit: int = 50,
) -> list[PayrollExportResponse]:
    """Return export history for the tenant.

    Requires ``export:datev`` permission.
    """
    _require_datev_permission(current_user)
    exports = await export_service.list_exports(db, current_user.tenant_id, skip, limit)
    return [PayrollExportResponse.model_validate(e) for e in exports]


@router.get("/datev/{export_id}/download")
async def download_export(
    export_id: uuid.UUID,
    db: DBSession,
    current_user: AuthUser,
) -> Response:
    """Download the generated DATEV CSV file.

    Requires ``export:datev`` permission.
    """
    _require_datev_permission(current_user)
    try:
        export_record = await export_service.get_export(db, current_user.tenant_id, export_id)
    except export_service.ExportError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Export not found"
        ) from exc

    # Original ausliefern (beim Generieren gespeichert) — kein Re-Generate:
    # so stimmt die Datei exakt mit file_checksum und dem damaligen Stand ueberein.
    if export_record.file_content is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Für diesen Export ist keine Datei gespeichert — bitte neu erzeugen.",
        )
    filename = export_record.file_name or (
        f"datev_export_{export_record.year_month.strftime('%Y_%m')}.csv"
    )
    return Response(
        content=export_record.file_content,
        media_type="text/csv; charset=windows-1252",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.delete("/datev/{export_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_export(export_id: uuid.UUID, db: DBSession, current_user: AuthUser) -> None:
    """Export stornieren (Historie-Eintrag löschen).

    Hebt die Neuabschluss-Sperre des Monats auf (auto_resettle blockiert,
    solange ein GENERATED-Export existiert). DELIVERED bleibt geschützt —
    was beim Steuerberater ist, wird nicht stillschweigend gelöscht.
    """
    _require_datev_permission(current_user)
    export = await db.get(PayrollExport, export_id)
    if export is None or export.tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Export not found")
    if export.status == ExportStatus.DELIVERED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Export ist als ausgeliefert markiert — erst Status klären.",
        )
    await db.delete(export)
    await db.commit()


@router.post("/datev/resettle")
async def resettle_export_month(body: dict, db: DBSession, current_user: AuthUser) -> dict:
    """Monatsabschluss für alle Mitarbeiter neu rechnen (nach Datenkorrekturen).

    body: {year, month}. Blockiert, solange für den Monat noch ein Export in
    der Historie liegt (erst stornieren) — sonst laufen Datei und Konto
    auseinander. Manuelle Korrekturen bleiben unangetastet.
    """
    from sqlalchemy import select as _select

    from zeiterfassung.models.user import User as _User
    from zeiterfassung.services import overtime_service

    _require_datev_permission(current_user)
    try:
        year = int(body["year"])
        month = int(body["month"])
        ym = date(year, month, 1)
    except (KeyError, ValueError, TypeError) as err:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="year/month fehlt."
        ) from err
    # Der laufende Monat wird nie abgeschlossen — er rechnet live; ein Abschluss
    # mitten im Monat friert Teilwerte ein und bucht vorzeitige Auszahlungen
    # (Fehlklick 24.08.2026: 38 Mitarbeiter, 13 vorzeitige Auszahlungen).
    heute = datetime.now(tz=UTC).date()
    if ym >= date(heute.year, heute.month, 1):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Der laufende Monat rechnet automatisch mit — neu berechnen geht ab "
                "dem 1. des Folgemonats."
            ),
        )
    existing = (
        (
            await db.execute(
                _select(PayrollExport).where(
                    PayrollExport.tenant_id == current_user.tenant_id,
                    PayrollExport.year_month == ym,
                    PayrollExport.status.in_([ExportStatus.GENERATED, ExportStatus.DELIVERED]),
                )
            )
        )
        .scalars()
        .all()
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Für diesen Monat existiert noch ein Export — bitte erst stornieren.",
        )
    users = (
        (
            await db.execute(
                _select(_User).where(
                    _User.tenant_id == current_user.tenant_id,
                    _User.is_active == True,  # noqa: E712
                )
            )
        )
        .scalars()
        .all()
    )
    done = 0
    for u in users:
        try:
            await overtime_service.resettle_month(
                db, current_user.tenant_id, u, year, month, actor_id=current_user.user_id
            )
            done += 1
        except Exception:  # noqa: BLE001 — einzelner MA stoppt den Lauf nicht
            continue
    await db.commit()
    return {"resettled": done, "year": year, "month": month}


@router.post("/datev/reopen")
async def reopen_month_endpoint(body: dict, db: DBSession, current_user: AuthUser) -> dict:
    """Versehentlichen Abschluss des LAUFENDEN Monats zurücknehmen.

    body: {year, month}. Löscht die Abschluss-Zeilen (Ansammlung, Auszahlung,
    Verfall, UEBERSTUNDEN_AUSZAHLUNG-Buchung) und schließt NICHT neu ab — der
    Monat rechnet danach wieder live und wird am Monatsende regulär
    abgeschlossen. Nur für den laufenden/zukünftigen Monat erlaubt: bei
    vergangenen Monaten würde die Ansammlung dauerhaft im Saldo fehlen —
    dort ist "Monat neu abschließen" der richtige Weg.
    """
    from sqlalchemy import select as _select

    from zeiterfassung.models.user import User as _User
    from zeiterfassung.services import overtime_service

    _require_datev_permission(current_user)
    try:
        year = int(body["year"])
        month = int(body["month"])
        ym = date(year, month, 1)
    except (KeyError, ValueError, TypeError) as err:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="year/month fehlt."
        ) from err
    heute = datetime.now(tz=UTC).date()
    if ym < date(heute.year, heute.month, 1):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Vergangene Monate nicht wieder öffnen — ihre Ansammlung würde "
                "dauerhaft im Saldo fehlen. Dort 'Monat neu abschließen' verwenden."
            ),
        )
    users = (
        (
            await db.execute(
                _select(_User).where(
                    _User.tenant_id == current_user.tenant_id,
                    _User.is_active == True,  # noqa: E712
                )
            )
        )
        .scalars()
        .all()
    )
    done = 0
    for u in users:
        try:
            await overtime_service.reopen_month(
                db, current_user.tenant_id, u, year, month, actor_id=current_user.user_id
            )
            done += 1
        except Exception:  # noqa: BLE001 — einzelner MA stoppt den Lauf nicht
            continue
    await db.commit()
    return {"reopened": done, "year": year, "month": month}


@router.get("/monthly-overview.xlsx")
async def monthly_overview_xlsx(
    db: DBSession,
    current_user: AuthUser,
    company_id: uuid.UUID,
    employment_type: str,
    year_from: int,
    month_from: int,
    year_to: int | None = None,
    month_to: int | None = None,
) -> Response:
    """Monatsuebersicht als Excel: die sechs Spalten der bisherigen Handliste.

    Ersetzt das Abtippen der Lohn-/Gehaltslisten — Personalnummer, Name,
    Urlaubsstand Vormonat/Monat, genommene Urlaubstage, Kranktage. Ein Blatt je
    Monat; ueber ``year_to``/``month_to`` mehrere Monate in einer Datei.
    Aufteilung wie die bestehenden vier Dateien: Firma x ``employment_type``
    (``hourly`` = Lohn, ``salaried`` = Gehalt).
    """
    _require_datev_permission(current_user)
    if employment_type not in ("hourly", "salaried"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="employment_type muss 'hourly' oder 'salaried' sein.",
        )
    y2, m2 = year_to or year_from, month_to or month_from
    if (y2, m2) < (year_from, month_from):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Zeitraum-Ende liegt vor dem Beginn."
        )

    from zeiterfassung.services import monthly_overview_service as mos
    from zeiterfassung.services.tenant_service import resolve_bundesland

    bundesland = await resolve_bundesland(db, current_user.tenant_id)
    months: list[tuple[int, int, list]] = []
    y, m = year_from, month_from
    while (y, m) <= (y2, m2) and len(months) < 24:
        rows = await mos.build_rows(
            db, current_user.tenant_id, company_id, employment_type, y, m, bundesland
        )
        months.append((y, m, rows))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)

    data = mos.build_workbook(months)
    art = "Lohn" if employment_type == "hourly" else "Gehalt"
    name = f"Monatsuebersicht_{art}_{year_from}-{month_from:02d}"
    if (y2, m2) != (year_from, month_from):
        name += f"_bis_{y2}-{m2:02d}"
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{name}.xlsx"'},
    )
