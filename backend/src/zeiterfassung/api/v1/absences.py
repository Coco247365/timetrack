"""Absence / leave request API endpoints."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from zeiterfassung.api.deps import AuthUser, DBSession
from zeiterfassung.core.errors import ErrorCode, api_error
from zeiterfassung.models.absence import RequestStatus, RequestType, SickStatus
from zeiterfassung.policy.context import DayPart
from zeiterfassung.schemas.absence import (
    AbsenceRequestCreate,
    AbsenceRequestResponse,
    ApproveRequest,
    RejectRequest,
    VacationBalance,
)
from zeiterfassung.services import absence_service

router = APIRouter()


def _maybe_strip_medical(
    response: AbsenceRequestResponse,
    has_medical_read: bool,
) -> AbsenceRequestResponse:
    """Strip sensitive sick-leave fields unless the caller has medical:read."""
    d = response.model_dump()
    absence_service.strip_medical_fields(d, has_medical_read)
    return AbsenceRequestResponse(**d)


@router.post(
    "/requests",
    response_model=AbsenceRequestResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_request(
    body: AbsenceRequestCreate,
    db: DBSession,
    current_user: AuthUser,
) -> AbsenceRequestResponse:
    """Submit a new leave request.

    Requires ``leave:request_own`` permission.
    Buchhaltung and Betriebsrat are intentionally excluded — they may not submit
    leave requests through this system per ADR-007.
    """
    if not current_user.has_permission("leave:request_own"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="leave:request_own permission required",
        )
    try:
        # Manager mit Genehmigungsbefugnis (GF/HR) tragen eigene Abwesenheiten
        # direkt ein (auto-genehmigt); alle anderen reichen zur Genehmigung ein.
        req = await absence_service.create_request(
            db,
            current_user.tenant_id,
            current_user.user_id,
            body,
            auto_approve=current_user.has_permission("leave:approve_all"),
        )
    except absence_service.ConflictError as exc:
        raise api_error(status.HTTP_409_CONFLICT, exc) from exc
    await db.commit()
    return AbsenceRequestResponse.model_validate(req)


class AdminAbsenceCreate(BaseModel):
    """Admin/HR records an absence FOR an employee (auto-approved).

    Unlike the self-service path this allows retroactive dates (someone called in
    sick yesterday) and targets a specific employee.
    """

    employee_id: uuid.UUID
    request_type: RequestType
    date_from: date
    date_to: date
    day_part: DayPart = DayPart.FULL
    # Explizite Stunden für Teil-Tag-Urlaub (optional); nur bei Einzeltag
    # (date_from == date_to), erzwungen im Service.
    hours: float | None = Field(default=None, gt=0, le=24)
    # Art der Krankmeldung (AU/LFZ) — nur bei SICK_LEAVE.
    sick_status: SickStatus | None = None
    notes: str | None = Field(default=None, max_length=1024)


@router.post(
    "/admin/requests",
    response_model=AbsenceRequestResponse,
    status_code=status.HTTP_201_CREATED,
)
async def admin_create_request(
    body: AdminAbsenceCreate,
    db: DBSession,
    current_user: AuthUser,
) -> AbsenceRequestResponse:
    """Record an absence (Urlaub/Krank) for an employee — admin/HR, auto-approved.

    Shows up immediately in the team calendar and the employee's balance.
    """
    can = (
        current_user.has_permission("leave:approve_all")
        or current_user.has_permission("users:write")
        or current_user.has_permission_namespace("admin")
    )
    if not can:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="leave:approve_all, users:write or admin permission required",
        )
    if body.date_to < body.date_from:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="date_to must be on or after date_from",
        )
    # Ziel-Mitarbeiter muss zum eigenen Mandanten gehoeren.
    from sqlalchemy import select as _select

    from zeiterfassung.db.scoping import tenant_scoped
    from zeiterfassung.models.user import User as _User

    emp = (
        await db.execute(
            tenant_scoped(_select(_User.id), _User, current_user.tenant_id).where(
                _User.id == body.employee_id
            )
        )
    ).first()
    if emp is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Mitarbeiter nicht gefunden"
        )
    # Bypass the self-service past-date guard (model_construct skips validators).
    data = AbsenceRequestCreate.model_construct(
        request_type=body.request_type,
        date_from=body.date_from,
        date_to=body.date_to,
        day_part=body.day_part,
        hours=body.hours,
        sick_status=body.sick_status,
        notes=body.notes,
        payload=None,
    )
    try:
        req = await absence_service.create_request(
            db,
            current_user.tenant_id,
            body.employee_id,
            data,
            auto_approve=True,
            approver_id=current_user.employee_id,
        )
    except absence_service.ConflictError as exc:
        raise api_error(status.HTTP_409_CONFLICT, exc) from exc
    await db.commit()
    return AbsenceRequestResponse.model_validate(req)


@router.get("/requests", response_model=list[AbsenceRequestResponse])
async def list_own_requests(
    db: DBSession,
    current_user: AuthUser,
    skip: int = 0,
    limit: int = 50,
    employee_id: uuid.UUID | None = None,
    # Query-Name "status" — Python-Name weicht ab, weil `status` hier das
    # fastapi.status-Modul ist.
    status_filter: Annotated[RequestStatus | None, Query(alias="status")] = None,
) -> list[AbsenceRequestResponse]:
    """List leave requests filtered by the caller's data visibility scope.

    - Global (GF/HR): all tenant requests
    - Department (Teamleiter): department's requests
    - Self (Mitarbeiter): own requests only
    - Anonymized (Betriebsrat): empty list

    ``employee_id`` engt zusätzlich zum Scope ein (AND) — für die
    Mitarbeiter-Detailseite, damit ältere Anträge nicht aus dem
    ``limit``-Fenster der tenant-weiten Liste fallen (#161).
    """
    scope = current_user.get_scope()
    has_medical = "medical:read" in current_user.permissions
    requests = await absence_service.list_requests(
        db,
        current_user.tenant_id,
        employee_id=employee_id,
        status=status_filter,
        skip=skip,
        limit=limit,
        scope=scope,
    )
    # Namen anreichern (fuer die Antraege-Uebersicht): nur Namen von Antraegen,
    # die der Aufrufer ohnehin sehen darf — kein zusaetzlicher Datenzugriff.
    from sqlalchemy import select as _select

    from zeiterfassung.models.user import User as _User

    emp_ids = {r.employee_id for r in requests}
    names: dict = {}
    if emp_ids:
        for u in (await db.execute(_select(_User).where(_User.id.in_(emp_ids)))).scalars().all():
            names[u.id] = (f"{u.first_name} {u.last_name}", u.employee_number)
    out = []
    for r in requests:
        resp = AbsenceRequestResponse.model_validate(r)
        nm = names.get(r.employee_id)
        if nm:
            resp.employee_name, resp.employee_number = nm
        out.append(_maybe_strip_medical(resp, has_medical))
    return out


@router.get("/requests/{request_id}", response_model=AbsenceRequestResponse)
async def get_request(
    request_id: uuid.UUID,
    db: DBSession,
    current_user: AuthUser,
) -> AbsenceRequestResponse:
    """Get detail of a single leave request.

    Access rules:
    - Own request: always allowed
    - Global scope (GF/HR): any tenant request
    - Department scope (Teamleiter): request must belong to same department
    - Self-only / Anonymized: own requests only
    """
    # Roles without any leave permission (e.g. Buchhaltung, Betriebsrat) may not
    # access individual absence requests — return 404 to avoid leaking existence (ADR-007).
    has_leave_access = (
        current_user.has_permission("leave:request_own")
        or current_user.has_permission("leave:approve_team")
        or current_user.has_permission("leave:approve_all")
    )
    if not has_leave_access:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found")

    try:
        req = await absence_service.get_request(db, current_user.tenant_id, request_id)
    except absence_service.RequestNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Request not found"
        ) from exc

    scope = current_user.get_scope()
    is_own = req.employee_id == current_user.user_id

    if not is_own:
        if scope.is_anonymized or scope.is_self_only:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found")
        if scope.is_department:
            # Verify the request owner is in the same department
            if not current_user.department_ids:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="Request not found"
                )
            from sqlalchemy import select

            from zeiterfassung.db.scoping import tenant_scoped
            from zeiterfassung.models.user import User

            result = await db.execute(
                tenant_scoped(select(User), User, current_user.tenant_id).where(
                    User.id == req.employee_id,
                    User.department_id.in_(current_user.department_ids),
                )
            )
            if result.scalar_one_or_none() is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="Request not found"
                )
        # is_global: access granted

    has_medical = "medical:read" in current_user.permissions
    response = AbsenceRequestResponse.model_validate(req)
    return _maybe_strip_medical(response, has_medical)


@router.patch("/requests/{request_id}/approve", response_model=AbsenceRequestResponse)
async def approve_request(
    request_id: uuid.UUID,
    body: ApproveRequest,
    db: DBSession,
    current_user: AuthUser,
) -> AbsenceRequestResponse:
    """Approve a leave request.

    Permission rules:
    - ``leave:approve_all`` (GF/HR): approve any request
    - ``leave:approve_team`` (Teamleiter): approve only requests in same department
    - Legacy exact ``admin``/``teamleiter`` permissions still accepted

    Security: Self-approval is always forbidden.
    Note: ``has_permission_namespace`` is intentionally NOT used — ``admin:dashboard``
    (Buchhaltung) must not grant approval authority (ADR-007).
    """
    has_approve_all = current_user.has_permission(
        "leave:approve_all"
    ) or current_user.has_permission("admin")
    has_approve_team = current_user.has_permission(
        "leave:approve_team"
    ) or current_user.has_permission("teamleiter")

    if not has_approve_all and not has_approve_team:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions"
        )

    if has_approve_team and not has_approve_all:
        # Verify the request belongs to someone in the approver's department
        try:
            req_check = await absence_service.get_request(db, current_user.tenant_id, request_id)
        except absence_service.RequestNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Request not found"
            ) from exc

        if not current_user.department_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No department assigned to your account",
            )
        from sqlalchemy import select

        from zeiterfassung.db.scoping import tenant_scoped
        from zeiterfassung.models.user import User

        result = await db.execute(
            tenant_scoped(select(User), User, current_user.tenant_id).where(
                User.id == req_check.employee_id,
                User.department_id.in_(current_user.department_ids),
            )
        )
        if result.scalar_one_or_none() is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Request does not belong to your department",
            )

    try:
        req = await absence_service.approve_request(
            db, current_user.tenant_id, request_id, current_user.user_id, body.reason
        )
    except absence_service.RequestNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Request not found"
        ) from exc
    except absence_service.InvalidStatusTransitionError as exc:
        raise api_error(status.HTTP_409_CONFLICT, exc) from exc
    except absence_service.SelfApprovalError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot approve your own absence request",
        ) from exc
    await db.commit()
    return AbsenceRequestResponse.model_validate(req)


@router.patch("/requests/{request_id}/reject", response_model=AbsenceRequestResponse)
async def reject_request(
    request_id: uuid.UUID,
    body: RejectRequest,
    db: DBSession,
    current_user: AuthUser,
) -> AbsenceRequestResponse:
    """Reject a leave request.

    Same permission rules as approve: ``leave:approve_all`` or ``leave:approve_team``
    (with department check).  Legacy exact ``admin`` permission accepted.
    Note: ``has_permission_namespace`` is intentionally NOT used — ``admin:dashboard``
    (Buchhaltung) must not grant rejection authority (ADR-007).
    """
    has_approve_all = current_user.has_permission(
        "leave:approve_all"
    ) or current_user.has_permission("admin")
    has_approve_team = current_user.has_permission(
        "leave:approve_team"
    ) or current_user.has_permission("teamleiter")

    if not has_approve_all and not has_approve_team:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions"
        )

    if has_approve_team and not has_approve_all:
        if not current_user.department_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No department assigned to your account",
            )
        try:
            req_check = await absence_service.get_request(db, current_user.tenant_id, request_id)
        except absence_service.RequestNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Request not found"
            ) from exc

        from sqlalchemy import select

        from zeiterfassung.db.scoping import tenant_scoped
        from zeiterfassung.models.user import User

        result = await db.execute(
            tenant_scoped(select(User), User, current_user.tenant_id).where(
                User.id == req_check.employee_id,
                User.department_id.in_(current_user.department_ids),
            )
        )
        if result.scalar_one_or_none() is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Request does not belong to your department",
            )

    try:
        req = await absence_service.reject_request(
            db, current_user.tenant_id, request_id, current_user.user_id, body.reason
        )
    except absence_service.RequestNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Request not found"
        ) from exc
    except absence_service.InvalidStatusTransitionError as exc:
        raise api_error(status.HTTP_409_CONFLICT, exc) from exc
    await db.commit()
    return AbsenceRequestResponse.model_validate(req)


@router.patch("/requests/{request_id}/cancel", response_model=AbsenceRequestResponse)
async def cancel_request(
    request_id: uuid.UUID,
    db: DBSession,
    current_user: AuthUser,
) -> AbsenceRequestResponse:
    """Cancel own leave request.

    Requires ``leave:request_own`` permission. Buchhaltung and Betriebsrat do not
    have this permission and may not cancel leave requests (ADR-007).
    Additionally, ownership is verified in the service layer — non-owners get 404.
    """
    if not current_user.has_permission("leave:request_own"):  # ADR-007 guard
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="leave:request_own permission required",
        )
    try:
        req = await absence_service.cancel_request(
            db, current_user.tenant_id, request_id, current_user.user_id
        )
    except absence_service.RequestNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Request not found"
        ) from exc
    except absence_service.InvalidStatusTransitionError as exc:
        raise api_error(status.HTTP_409_CONFLICT, exc) from exc
    await db.commit()
    return AbsenceRequestResponse.model_validate(req)


@router.patch("/requests/{request_id}", response_model=AbsenceRequestResponse)
async def update_request_period(
    request_id: uuid.UUID,
    body: dict,
    db: DBSession,
    current_user: AuthUser,
) -> AbsenceRequestResponse:
    """Zeitraum einer Abwesenheit aendern (frueher zurueck / verlaengert).

    Gleiche Rechte wie Genehmigen: leave:approve_all, leave:approve_team
    (nur eigener Bereich), users:write oder Admin.
    """
    if not (
        current_user.has_permission("leave:approve_all")
        or current_user.has_permission("leave:approve_team")
        or current_user.has_permission("users:write")
        or current_user.has_permission_namespace("admin")
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Genehmigungs-Recht erforderlich",
        )
    # Teamleiter ohne globales Recht: Ziel muss im eigenen Bereich liegen.
    if (
        current_user.has_permission("leave:approve_team")
        and not current_user.has_permission("leave:approve_all")
        and not current_user.has_permission_namespace("admin")
    ):
        from sqlalchemy import select

        from zeiterfassung.models.user import User

        req0 = await absence_service.get_request(db, current_user.tenant_id, request_id)
        if not current_user.department_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No department assigned to your account",
            )
        ok = (
            await db.execute(
                select(User.id).where(
                    User.id == req0.employee_id,
                    User.department_id.in_(current_user.department_ids),
                )
            )
        ).first()
        if ok is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Antrag liegt nicht in Ihrem Bereich"
            )

    from datetime import date as _date

    try:
        df = _date.fromisoformat(str(body.get("date_from"))[:10])
        dt = _date.fromisoformat(str(body.get("date_to"))[:10])
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="date_from und date_to (YYYY-MM-DD) erforderlich",
        ) from None
    try:
        req = await absence_service.update_request_period(
            db, current_user.tenant_id, request_id, df, dt, current_user.user_id
        )
    except absence_service.RequestNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Request not found"
        ) from exc
    except absence_service.InvalidStatusTransitionError as exc:
        raise api_error(status.HTTP_409_CONFLICT, exc) from exc
    except absence_service.ConflictError as exc:
        raise api_error(status.HTTP_409_CONFLICT, exc) from exc
    await db.commit()
    return AbsenceRequestResponse.model_validate(req)


class SickStatusUpdate(BaseModel):
    """AU/LFZ-Status einer Krankmeldung setzen/ändern (null = löschen)."""

    sick_status: SickStatus | None = None


@router.patch("/requests/{request_id}/sick-status", response_model=AbsenceRequestResponse)
async def update_sick_status(
    request_id: uuid.UUID,
    body: SickStatusUpdate,
    db: DBSession,
    current_user: AuthUser,
) -> AbsenceRequestResponse:
    """Art der Krankmeldung (AU/LFZ) setzen — beim Erstellen ODER nachträglich.

    Gleiche Rechte wie Genehmigen/Zeitraum-Ändern.
    """
    if not (
        current_user.has_permission("leave:approve_all")
        or current_user.has_permission("leave:approve_team")
        or current_user.has_permission("users:write")
        or current_user.has_permission_namespace("admin")
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Genehmigungs-Recht erforderlich",
        )
    # Teamleiter (nur leave:approve_team, kein globales Recht): Ziel muss im
    # eigenen Bereich liegen — sonst koennte ein Teamleiter Krankmeldungen
    # fremder Bereiche bearbeiten.
    if (
        current_user.has_permission("leave:approve_team")
        and not current_user.has_permission("leave:approve_all")
        and not current_user.has_permission_namespace("admin")
    ):
        from sqlalchemy import select

        from zeiterfassung.db.scoping import tenant_scoped
        from zeiterfassung.models.user import User

        try:
            req_check = await absence_service.get_request(db, current_user.tenant_id, request_id)
        except absence_service.RequestNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Request not found"
            ) from exc
        if not current_user.department_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No department assigned to your account",
            )
        ok = (
            await db.execute(
                tenant_scoped(select(User.id), User, current_user.tenant_id).where(
                    User.id == req_check.employee_id,
                    User.department_id.in_(current_user.department_ids),
                )
            )
        ).first()
        if ok is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Antrag liegt nicht in Ihrem Bereich"
            )
    try:
        req = await absence_service.set_sick_status(
            db, current_user.tenant_id, request_id, body.sick_status, current_user.user_id
        )
    except absence_service.RequestNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Request not found"
        ) from exc
    except absence_service.ConflictError as exc:
        raise api_error(status.HTTP_409_CONFLICT, exc) from exc
    await db.commit()
    return AbsenceRequestResponse.model_validate(req)


@router.post("/requests/{request_id}/revoke", response_model=AbsenceRequestResponse)
async def revoke_request(
    request_id: uuid.UUID,
    body: RejectRequest,
    db: DBSession,
    current_user: AuthUser,
) -> AbsenceRequestResponse:
    """Revoke a previously approved absence request (TL/HR action).

    Used when an employee shows up to work during approved vacation, or when
    circumstances change. Requires ``leave:approve_all`` (GF/HR) or
    ``leave:approve_team`` (Teamleiter, same department).

    Creates an audit trail entry documenting the revocation.
    """
    if not (
        current_user.has_permission("leave:approve_all")
        or current_user.has_permission("leave:approve_team")
        or current_user.has_permission("users:write")
        or current_user.has_permission_namespace("admin")
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Approval permission required to revoke",
        )
    # Teamleiter (nur leave:approve_team, kein globales Recht): Ziel muss im
    # eigenen Bereich liegen — sonst koennte ein Teamleiter Genehmigungen
    # fremder Bereiche widerrufen.
    if (
        current_user.has_permission("leave:approve_team")
        and not current_user.has_permission("leave:approve_all")
        and not current_user.has_permission_namespace("admin")
    ):
        from sqlalchemy import select

        from zeiterfassung.db.scoping import tenant_scoped
        from zeiterfassung.models.user import User

        try:
            req_check = await absence_service.get_request(db, current_user.tenant_id, request_id)
        except absence_service.RequestNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Request not found"
            ) from exc
        if not current_user.department_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No department assigned to your account",
            )
        ok = (
            await db.execute(
                tenant_scoped(select(User.id), User, current_user.tenant_id).where(
                    User.id == req_check.employee_id,
                    User.department_id.in_(current_user.department_ids),
                )
            )
        ).first()
        if ok is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Antrag liegt nicht in Ihrem Bereich"
            )
    try:
        req = await absence_service.revoke_approval(
            db,
            current_user.tenant_id,
            request_id,
            current_user.user_id,
            body.reason,
        )
    except absence_service.RequestNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Request not found"
        ) from exc
    except absence_service.InvalidStatusTransitionError as exc:
        raise api_error(status.HTTP_409_CONFLICT, exc) from exc
    await db.commit()
    return AbsenceRequestResponse.model_validate(req)


@router.post("/requests/{request_id}/request-cancellation", response_model=AbsenceRequestResponse)
async def request_cancellation(
    request_id: uuid.UUID,
    body: ApproveRequest,
    db: DBSession,
    current_user: AuthUser,
) -> AbsenceRequestResponse:
    """Employee requests cancellation of an own approved, not-yet-past request."""
    if not current_user.has_permission("leave:request_own"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="leave:request_own required"
        )
    try:
        req = await absence_service.request_cancellation(
            db, current_user.tenant_id, request_id, current_user.user_id, body.reason
        )
    except absence_service.RequestNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Request not found"
        ) from exc
    except absence_service.InvalidStatusTransitionError as exc:
        raise api_error(status.HTTP_409_CONFLICT, exc) from exc
    await db.commit()
    return AbsenceRequestResponse.model_validate(req)


@router.get("/cancellation-requests", response_model=list[dict])
async def cancellation_requests(db: DBSession, current_user: AuthUser) -> list[dict]:
    """Pending employee cancellation requests on approved leave (approvers only)."""
    if not (
        current_user.has_permission("leave:approve_all")
        or current_user.has_permission("leave:approve_team")
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Approval permission required"
        )
    reqs = await absence_service.list_cancellation_requests(
        db, current_user.tenant_id, current_user.get_scope()
    )
    from sqlalchemy import select as _select

    from zeiterfassung.models.user import User

    names: dict = {}
    if reqs:
        ids = [r.employee_id for r in reqs]
        rows = await db.execute(_select(User).where(User.id.in_(ids)))
        names = {u.id: f"{u.first_name} {u.last_name}" for u in rows.scalars().all()}
    out = []
    for r in reqs:
        out.append(
            {
                "id": str(r.id),
                "employee_name": names.get(r.employee_id, "?"),
                "request_type": r.request_type.value
                if hasattr(r.request_type, "value")
                else str(r.request_type),
                "date_from": r.date_from.isoformat(),
                "date_to": r.date_to.isoformat(),
                "days_count": float(r.days_count),
                "cancel_reason": (r.payload or {}).get("cancel_reason"),
            }
        )
    return out


@router.post("/requests/{request_id}/decline-cancellation", response_model=AbsenceRequestResponse)
async def decline_cancellation(
    request_id: uuid.UUID,
    db: DBSession,
    current_user: AuthUser,
) -> AbsenceRequestResponse:
    """Approver declines a cancellation request; the leave stays approved."""
    if not (
        current_user.has_permission("leave:approve_all")
        or current_user.has_permission("leave:approve_team")
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Approval permission required"
        )
    try:
        req = await absence_service.decline_cancellation(
            db, current_user.tenant_id, request_id, current_user.user_id
        )
    except absence_service.RequestNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Request not found"
        ) from exc
    await db.commit()
    return AbsenceRequestResponse.model_validate(req)


@router.get("/balance", response_model=VacationBalance)
async def get_balance(
    year: int,
    db: DBSession,
    current_user: AuthUser,
    employee_id: uuid.UUID | None = None,
) -> VacationBalance:
    """Return vacation balance.

    Without ``employee_id``: returns the caller's own balance (all roles).
    With ``employee_id``: returns another employee's balance — requires global
    or department scope (GF, HR, Teamleiter for same-department employees).
    """
    target = employee_id or current_user.user_id
    if target == current_user.user_id:
        return await absence_service.get_vacation_balance(db, current_user.tenant_id, target, year)
    scope = current_user.get_scope()
    try:
        return await absence_service.get_vacation_balance_for_scope(
            db, current_user.tenant_id, target, year, scope
        )
    except PermissionError as exc:
        raise api_error(
            status.HTTP_403_FORBIDDEN, exc, code=ErrorCode.VACATION_BALANCE_FORBIDDEN
        ) from exc


@router.get("/balances", response_model=dict[str, float])
async def get_balances(
    year: int,
    db: DBSession,
    current_user: AuthUser,
) -> dict[str, float]:
    """Resturlaub (Tage) je Mitarbeiter — für die Mitarbeiterliste.

    Gleiche Rechte wie die Anwesenheitsliste: ``timesheet:view_all`` (ganzer
    Mandant) bzw. ``timesheet:view_team`` (eigene Abteilung). Buchhaltung und
    Betriebsrat bleiben außen vor (ADR-007).
    """
    if not (
        current_user.has_permission("timesheet:view_all")
        or current_user.has_permission("timesheet:view_team")
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="timesheet:view_all or timesheet:view_team permission required",
        )
    balances = await absence_service.remaining_days_for_scope(
        db, current_user.tenant_id, year, current_user.get_scope()
    )
    return {str(eid): float(days) for eid, days in balances.items()}


@router.get("/team-capacity")
async def get_team_capacity(
    date_from: date,
    date_to: date,
    db: DBSession,
    current_user: AuthUser,
) -> dict:
    """Check team absence capacity for a date range.

    Uses the current user's department_id. Returns empty result if no department
    is assigned. Any authenticated user may call this endpoint to check team
    availability before submitting a leave request.
    """
    if not current_user.department_id:
        return {
            "total_employees": 0,
            "absent_count": 0,
            "absent_names": [],
            "capacity_pct": 0.0,
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
        }
    return await absence_service.team_capacity_check(
        db,
        current_user.tenant_id,
        current_user.department_id,
        date_from,
        date_to,
        exclude_employee_id=current_user.user_id,
    )


@router.get("/calendar", response_model=list[AbsenceRequestResponse])
async def get_calendar(
    date_from: date,
    date_to: date,
    db: DBSession,
    current_user: AuthUser,
    employee_id: uuid.UUID | None = None,
) -> list[AbsenceRequestResponse]:
    """Return absence calendar filtered by the caller's scope.

    - Global (GF/HR): all tenant absences
    - Department (Teamleiter): department absences
    - Self (Mitarbeiter): own absences
    - Anonymized (Betriebsrat): empty list

    Medical data (notes, payload) is stripped unless the caller has
    ``medical:read``.
    """
    scope = current_user.get_scope()
    has_medical = "medical:read" in current_user.permissions
    requests = await absence_service.get_calendar(
        db, current_user.tenant_id, date_from, date_to, scope=scope, employee_id=employee_id
    )
    return [
        _maybe_strip_medical(AbsenceRequestResponse.model_validate(r), has_medical)
        for r in requests
    ]


@router.get("/team-calendar", response_model=dict)
async def get_team_calendar(
    date_from: date,
    date_to: date,
    db: DBSession,
    current_user: AuthUser,
    include_unexcused: bool = False,
) -> dict:
    """Schlanker Teamkalender: Namen + Abwesenheiten, KEINE Zeiten/Stammdaten.

    Eigene Rechte, getrennt von den Arbeitszeit-Rechten, damit auch einfache
    Mitarbeiter den Urlaubs-/Abwesenheitskalender sehen koennen, ohne Zugriff
    auf Arbeitszeiten oder die Personalverwaltung zu bekommen:

    - ``calendar:view_all``  -> ganze Firma
    - ``calendar:view_team`` -> nur der eigene Bereich
    - Bestehende breite Rechte (admin, users:read_*, timesheet:view_*) zaehlen
      weiter, damit vorhandene Rollen nicht brechen.

    Datenschutz: Krankmeldungen erscheinen ohne ``medical:read`` nur als
    neutrales "abwesend"; Geburtstage nur als Monat-Tag (ohne Jahrgang).
    """
    from sqlalchemy import select

    from zeiterfassung.db.scoping import tenant_scoped
    from zeiterfassung.models.user import User

    view_all = (
        current_user.has_permission("calendar:view_all")
        or current_user.has_permission("users:read_all")
        or current_user.has_permission("timesheet:view_all")
        or current_user.has_permission_namespace("admin")
    )
    view_team = (
        current_user.has_permission("calendar:view_team")
        or current_user.has_permission("users:read_team")
        or current_user.has_permission("timesheet:view_team")
    )
    if not view_all and not view_team:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="calendar:view_team oder calendar:view_all erforderlich",
        )

    emp_stmt = tenant_scoped(select(User), User, current_user.tenant_id).where(
        User.is_active == True,  # noqa: E712
    )
    if not view_all:
        # Nur der eigene Bereich; ohne Bereich sieht man nur sich selbst.
        if current_user.department_ids:
            emp_stmt = emp_stmt.where(User.department_id.in_(current_user.department_ids))
        else:
            emp_stmt = emp_stmt.where(User.id == current_user.user_id)
    employees = list((await db.execute(emp_stmt)).scalars().all())

    from zeiterfassung.models.company import Company
    from zeiterfassung.models.department import Department

    dept_names = {
        d.id: d.name
        for d in (
            await db.execute(tenant_scoped(select(Department), Department, current_user.tenant_id))
        )
        .scalars()
        .all()
    }
    company_names = {
        c.id: c.name
        for c in (await db.execute(tenant_scoped(select(Company), Company, current_user.tenant_id)))
        .scalars()
        .all()
    }

    emp_ids = {u.id for u in employees}
    # Abwesenheiten ueber den bestehenden Kalender-Service holen, aber fuer die
    # Team-Sicht bewusst OHNE dessen Self-Scope-Filter (das eigene Recht oben
    # regelt die Sichtbarkeit); danach auf die sichtbaren Mitarbeiter filtern.
    all_requests = await absence_service.get_calendar(
        db, current_user.tenant_id, date_from, date_to, scope=None
    )
    has_medical = "medical:read" in current_user.permissions

    def _kind(request_type: str) -> str:
        rt = str(request_type)
        if "VACATION" in rt:
            return "urlaub"
        if "SICK" in rt:
            return "krank" if has_medical else "abwesend"
        # Neue Arten (Spec 2026-08-11): eigene Farben im Team-Kalender,
        # keine Medical-Schranke — beide sind organisatorisch, nicht sensibel.
        if "SPECIAL_LEAVE" in rt:
            return "sonderurlaub"
        if "UNPAID_LEAVE" in rt:
            return "unbezahlt"
        if "OVERTIME_REDUCTION" in rt:
            return "abbau"
        return "abwesend"

    absences = [
        {
            "employee_id": str(r.employee_id),
            "kind": _kind(r.request_type),
            "date_from": r.date_from.isoformat(),
            "date_to": r.date_to.isoformat(),
            "status": str(r.status.value if hasattr(r.status, "value") else r.status),
        }
        for r in all_requests
        if r.employee_id in emp_ids
    ]

    # Vergangene Arbeitstage (net_work_minutes > 0) fuer die Gruen-Markierung
    # im Teamkalender — nur wenn das Feature aktiv ist (#187). Heute
    # bleibt der Live-Anwesenheit vorbehalten und wird hier NICHT geliefert.
    from datetime import date as _date

    from zeiterfassung.models.clock import DailyTimesheet
    from zeiterfassung.services import admin_settings_service

    # Wer Arbeitszeiten sehen darf, bekommt zusaetzlich Beginn/Ende je Tag (fuer
    # "gearbeitet von … bis …" im Tooltip) und den Erfassungsmodus. Der
    # Teamkalender ist auch fuer einfache Mitarbeiter sichtbar — die sehen
    # weiterhin nur, DASS gearbeitet wurde, nicht wann.
    may_see_worktime = (
        current_user.has_permission("timesheet:view_all")
        or current_user.has_permission("timesheet:view_team")
        or current_user.has_permission_namespace("admin")
    )

    worked: list[dict] = []
    features = await admin_settings_service.get_features(db, current_user.tenant_id)
    if features.get("team_past_worked"):
        today = _date.today()
        # Inkl. HEUTE: wer heute schon Arbeitszeit erfasst hat (auch bereits
        # ausgestempelt oder manuell nachgetragen) bekommt einen gruenen Punkt,
        # nicht nur wer gerade LIVE eingestempelt ist (#192). Zukunft aus.
        stmt = tenant_scoped(select(DailyTimesheet), DailyTimesheet, current_user.tenant_id).where(
            DailyTimesheet.work_date >= date_from,
            DailyTimesheet.work_date <= today,
            DailyTimesheet.net_work_minutes > 0,
        )
        # Nur echte Stempeltage: Auto-/manuell gesetzte Tage (computed_from_events
        # = False) ausblenden — sonst waeren Auto-MA jeden Werktag gruen (#191).
        if features.get("team_past_worked_stamped_only"):
            stmt = stmt.where(DailyTimesheet.computed_from_events == True)  # noqa: E712
        rows = await db.execute(stmt)
        worked = [
            {
                "employee_id": str(r.employee_id),
                "date": r.work_date.isoformat(),
                **(
                    {
                        "start": r.start_time.isoformat() if r.start_time else None,
                        "end": r.end_time.isoformat() if r.end_time else None,
                    }
                    if may_see_worktime
                    else {}
                ),
            }
            for r in rows.scalars().all()
            if r.employee_id in emp_ids
        ]

    # Vergessenes Ausstempeln (Spec 2026-08-03): Tage, die automatisch nach
    # 14 h gekappt und noch NICHT von HR aufgeloest wurden, im Teamkalender mit
    # Warnsymbol markieren. Nur fuer Rollen, die Arbeitszeiten ohnehin sehen
    # duerfen — der Teamkalender ist auch fuer einfache Mitarbeiter sichtbar und
    # zeigt bewusst keine Zeit-Informationen.
    flagged: list[dict] = []
    unexcused: list[dict] = []
    if may_see_worktime:
        from zeiterfassung.services.clock_service import FORGOTTEN_CLOCKOUT_FLAG

        flag_stmt = tenant_scoped(
            select(DailyTimesheet), DailyTimesheet, current_user.tenant_id
        ).where(
            DailyTimesheet.work_date >= date_from,
            DailyTimesheet.work_date <= date_to,
            DailyTimesheet.flagged_reason.contains(FORGOTTEN_CLOCKOUT_FLAG),
        )
        flagged = [
            {"employee_id": str(r.employee_id), "date": r.work_date.isoformat()}
            for r in (await db.execute(flag_stmt)).scalars().all()
            if r.employee_id in emp_ids
        ]

        # Unentschuldigte Fehltage (rotes „U" im Teamkalender) — gleiche
        # Tagesregel wie im Mitarbeiter-Verlauf (policy.overtime.classify_day).
        # Nur auf Anforderung (``include_unexcused``): die Berechnung kostet je
        # Mitarbeiter und Monat einen Durchlauf; ist die Anzeige im Kalender
        # ausgeschaltet, faellt sie ganz weg.
        if include_unexcused:
            from zeiterfassung.services import overtime_service

            unexcused = [
                {"employee_id": str(uid), "date": d.isoformat()}
                for uid, d in await overtime_service.unexcused_days(
                    db, current_user.tenant_id, employees, date_from, date_to
                )
            ]

    return {
        "employees": [
            {
                "id": str(u.id),
                "name": f"{u.first_name} {u.last_name}",
                "department_name": dept_names.get(u.department_id),
                "company_name": company_names.get(u.company_id),
                # Monat-Tag + Geburtsjahr (fürs Alter im Teamkalender).
                "birthday_md": u.birth_date.strftime("%m-%d") if u.birth_date else None,
                "birth_year": u.birth_date.year if u.birth_date else None,
                # Firmenjubiläum: Monat-Tag + Eintrittsjahr (für "X Jahre").
                "hire_md": u.hire_date.strftime("%m-%d") if u.hire_date else None,
                "hire_year": u.hire_date.year if u.hire_date else None,
                # Erfassungsmodus — markiert im Tooltip Zeiten aus der
                # automatischen Erfassung als "(auto)". Nur fuer Rollen, die
                # ohnehin Arbeitszeiten sehen; sonst hat der Marker keinen Bezug.
                **({"time_capture": u.time_capture} if may_see_worktime else {}),
            }
            for u in sorted(employees, key=lambda u: (u.last_name, u.first_name))
        ],
        "absences": absences,
        "worked": worked,
        "flagged": flagged,
        "unexcused": unexcused,
    }


# ---------------------------------------------------------------------------
# Krankmeldungen per E-Mail (Gesundheitsdaten -> medical:read)
# ---------------------------------------------------------------------------


def _can_view_sick_mails(u: AuthUser) -> bool:  # type: ignore[name-defined]
    return u.has_permission("medical:read") or u.has_permission_namespace("admin")


async def _require_sick_mail_enabled(db, tenant_id) -> None:
    """403, wenn die Funktion in den Einstellungen deaktiviert ist."""
    from sqlalchemy import select as _select

    from zeiterfassung.models.tenant import Tenant

    tenant = (await db.execute(_select(Tenant).where(Tenant.id == tenant_id))).scalar_one_or_none()
    features = ((tenant.settings or {}).get("features") or {}) if tenant else {}
    if features.get("sick_mail", True) is False:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Krankmeldungen per E-Mail sind in den Einstellungen deaktiviert.",
        )


@router.get("/sick-mails", response_model=list[dict])
async def list_sick_mails(db: DBSession, current_user: AuthUser) -> list[dict]:
    """Per E-Mail eingegangene Krankmeldungen (neueste zuerst)."""
    if not _can_view_sick_mails(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="medical:read erforderlich"
        )
    await _require_sick_mail_enabled(db, current_user.tenant_id)
    from sqlalchemy import select

    from zeiterfassung.db.scoping import tenant_scoped
    from zeiterfassung.models.sickmail import SickMailReport
    from zeiterfassung.models.user import User

    reports = list(
        (
            await db.execute(
                tenant_scoped(select(SickMailReport), SickMailReport, current_user.tenant_id)
                # Bewusste Obergrenze (neueste zuerst) — KEINE Pagination:
                # ab dem 501. Eintrag fallen die ältesten aus der Liste.
                # Wenn das real erreicht wird, Pagination nachrüsten.
                .order_by(SickMailReport.received_at.desc())
                .limit(500)
            )
        )
        .scalars()
        .all()
    )
    emp_ids = {r.employee_id for r in reports if r.employee_id}
    names: dict = {}
    if emp_ids:
        for u in (await db.execute(select(User).where(User.id.in_(emp_ids)))).scalars().all():
            names[u.id] = f"{u.first_name} {u.last_name}"
    return [
        {
            "id": str(r.id),
            "sender_email": r.sender_email,
            "subject": r.subject,
            "body_preview": r.body_preview,
            "received_at": r.received_at.isoformat(),
            "parsed_date_from": r.parsed_date_from.isoformat() if r.parsed_date_from else None,
            "parsed_date_to": r.parsed_date_to.isoformat() if r.parsed_date_to else None,
            "parsed_hint": r.parsed_hint,
            "employee_id": str(r.employee_id) if r.employee_id else None,
            "employee_name": names.get(r.employee_id),
            "absence_request_id": str(r.absence_request_id) if r.absence_request_id else None,
            "status": r.status,
            "assignment": r.assignment,
        }
        for r in reports
    ]


@router.post("/sick-mails/{report_id}/assign", response_model=dict)
async def assign_sick_mail(
    report_id: uuid.UUID, body: dict, db: DBSession, current_user: AuthUser
) -> dict:
    """Unbekannten Absender manuell einem Mitarbeiter zuordnen (erzeugt Antrag)."""
    if not _can_view_sick_mails(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="medical:read erforderlich"
        )
    await _require_sick_mail_enabled(db, current_user.tenant_id)
    from zeiterfassung.services import sickmail_service

    emp = body.get("employee_id")
    if not emp:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="employee_id erforderlich"
        )
    from datetime import date as _date

    df = body.get("date_from")
    dt = body.get("date_to")
    try:
        report = await sickmail_service.assign_report(
            db,
            current_user.tenant_id,
            report_id,
            uuid.UUID(str(emp)),
            date_from=_date.fromisoformat(str(df)[:10]) if df else None,
            date_to=_date.fromisoformat(str(dt)[:10]) if dt else None,
        )
    except LookupError as exc:
        raise api_error(status.HTTP_404_NOT_FOUND, exc, code=ErrorCode.SICK_MAIL_NOT_FOUND) from exc
    except ValueError as exc:
        raise api_error(status.HTTP_409_CONFLICT, exc, code=ErrorCode.SICK_MAIL_CONFLICT) from exc
    await db.commit()
    return {"ok": True, "status": report.status}


@router.post("/sick-mails/{report_id}/dismiss", response_model=dict)
async def dismiss_sick_mail(report_id: uuid.UUID, db: DBSession, current_user: AuthUser) -> dict:
    """Fehlgeleitete Mail verwerfen."""
    if not _can_view_sick_mails(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="medical:read erforderlich"
        )
    await _require_sick_mail_enabled(db, current_user.tenant_id)
    from zeiterfassung.services import sickmail_service

    try:
        await sickmail_service.dismiss_report(db, current_user.tenant_id, report_id)
    except LookupError as exc:
        raise api_error(status.HTTP_404_NOT_FOUND, exc, code=ErrorCode.SICK_MAIL_NOT_FOUND) from exc
    await db.commit()
    return {"ok": True}


@router.delete("/sick-mails/{report_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_sick_mail(report_id: uuid.UUID, db: DBSession, current_user: AuthUser) -> None:
    """Meldung endgueltig loeschen (Spam/Fehleingang)."""
    if not _can_view_sick_mails(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="medical:read erforderlich"
        )
    await _require_sick_mail_enabled(db, current_user.tenant_id)
    from zeiterfassung.services import sickmail_service

    try:
        await sickmail_service.delete_report(
            db, current_user.tenant_id, report_id, current_user.user_id
        )
    except LookupError as exc:
        raise api_error(status.HTTP_404_NOT_FOUND, exc, code=ErrorCode.SICK_MAIL_NOT_FOUND) from exc
    await db.commit()


@router.post("/sick-mails/{report_id}/reset", response_model=dict)
async def reset_sick_mail(report_id: uuid.UUID, db: DBSession, current_user: AuthUser) -> dict:
    """Zugeordnete/verworfene Meldung zurueck auf 'Offen' (storniert den Antrag)."""
    if not _can_view_sick_mails(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="medical:read erforderlich"
        )
    await _require_sick_mail_enabled(db, current_user.tenant_id)
    from zeiterfassung.services import sickmail_service

    try:
        report = await sickmail_service.reset_report(
            db, current_user.tenant_id, report_id, current_user.user_id
        )
    except LookupError as exc:
        raise api_error(status.HTTP_404_NOT_FOUND, exc, code=ErrorCode.SICK_MAIL_NOT_FOUND) from exc
    except ValueError as exc:
        raise api_error(status.HTTP_409_CONFLICT, exc, code=ErrorCode.SICK_MAIL_CONFLICT) from exc
    await db.commit()
    return {"ok": True, "status": report.status}


@router.post("/sick-mails/poll", response_model=dict)
async def poll_sick_mails(db: DBSession, current_user: AuthUser) -> dict:
    """Postfach sofort abrufen (statt auf den naechsten Poll zu warten)."""
    if not _can_view_sick_mails(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="medical:read erforderlich"
        )
    await _require_sick_mail_enabled(db, current_user.tenant_id)
    from zeiterfassung.services import sickmail_service

    try:
        imported = await sickmail_service.fetch_mailbox(db)
    except Exception as exc:  # noqa: BLE001 — Konfig-/Netzfehler sauber melden
        raise api_error(
            status.HTTP_502_BAD_GATEWAY, exc, code=ErrorCode.SICK_MAIL_FETCH_FAILED
        ) from exc
    return {"ok": True, "imported": imported}


# ---------------------------------------------------------------------------
# Kalender-Export (.ics): Download (eingeloggt) + Abo-Feed (Token)
# ---------------------------------------------------------------------------


async def _ics_events_for(db, tenant_id, employee_filter_ids: set | None, mask_sick: bool) -> str:
    """Genehmigte Abwesenheiten (letzter Monat bis +1 Jahr) als ICS-Text."""
    from datetime import timedelta

    from sqlalchemy import select

    from zeiterfassung.core.ics import IcsEvent, build_ics
    from zeiterfassung.db.scoping import tenant_scoped
    from zeiterfassung.models.absence import ApprovalRequest as _AR
    from zeiterfassung.models.absence import RequestStatus as _RS
    from zeiterfassung.models.user import User

    today = date.today()
    requests = list((await db.execute(tenant_scoped(select(_AR), _AR, tenant_id))).scalars().all())
    requests = [
        r
        for r in requests
        if r.status == _RS.APPROVED
        and r.date_to >= today - timedelta(days=31)
        and r.date_from <= today + timedelta(days=366)
        and (employee_filter_ids is None or r.employee_id in employee_filter_ids)
    ]
    emp_ids = {r.employee_id for r in requests}
    names: dict = {}
    if emp_ids:
        for u in (await db.execute(select(User).where(User.id.in_(emp_ids)))).scalars().all():
            names[u.id] = f"{u.first_name} {u.last_name}"

    def _label(r) -> str:
        rt = str(r.request_type)
        name = names.get(r.employee_id, "Unbekannt")
        if "VACATION" in rt:
            return f"{name} — Urlaub"
        if "SICK" in rt:
            return f"{name} — Krank" if not mask_sick else f"{name} — Abwesend"
        return f"{name} — Abwesend"

    events = [
        IcsEvent(
            uid=str(r.id),
            summary=_label(r),
            date_from=r.date_from,
            date_to=r.date_to,
            sequence=int(r.updated_at.timestamp()) if r.updated_at else 0,
        )
        for r in sorted(requests, key=lambda r: r.date_from)
    ]
    return build_ics(events, calendar_name="Abwesenheiten")


@router.get("/calendar.ics")
async def download_calendar_ics(db: DBSession, current_user: AuthUser):
    """ICS-Download fuer den eingeloggten Nutzer (gleiche Sicht wie Teamkalender)."""
    from fastapi.responses import Response

    view_all = (
        current_user.has_permission("calendar:view_all")
        or current_user.has_permission("users:read_all")
        or current_user.has_permission("timesheet:view_all")
        or current_user.has_permission_namespace("admin")
    )
    view_team = (
        current_user.has_permission("calendar:view_team")
        or current_user.has_permission("users:read_team")
        or current_user.has_permission("timesheet:view_team")
    )
    if not view_all and not view_team:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="calendar:view_team oder calendar:view_all erforderlich",
        )
    employee_filter: set | None = None
    if not view_all:
        from sqlalchemy import select

        from zeiterfassung.db.scoping import tenant_scoped
        from zeiterfassung.models.user import User

        stmt = tenant_scoped(select(User.id), User, current_user.tenant_id).where(
            User.is_active == True,  # noqa: E712
        )
        if current_user.department_ids:
            stmt = stmt.where(User.department_id.in_(current_user.department_ids))
        else:
            stmt = stmt.where(User.id == current_user.user_id)
        employee_filter = {r[0] for r in (await db.execute(stmt)).all()}

    mask_sick = "medical:read" not in current_user.permissions
    ics = await _ics_events_for(db, current_user.tenant_id, employee_filter, mask_sick)
    return Response(
        content=ics,
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="abwesenheiten.ics"'},
    )


@router.get("/feed.ics")
async def calendar_feed_ics(token: str, db: DBSession):
    """Abo-Feed fuer Kalender-Programme (Outlook Desktop, Apple Kalender).

    Kalender-Clients koennen keine Login-Header senden — deshalb ein
    geheimer Token in der URL (in den Einstellungen erzeugbar/rotierbar).
    Firmenweite Sicht; Krankmeldungen erscheinen IMMER nur als "Abwesend".
    """
    from fastapi.responses import Response
    from sqlalchemy import select

    from zeiterfassung.models.tenant import Tenant

    # Mandant WIRD ueber den Token aufgeloest (nicht "irgendein" Tenant) — sonst
    # serviert ein gueltiger Token von Mandant B den Kalender von Mandant A
    # (erster Tenant in der Tabelle). ``settings`` ist JSON/JSONB (SQLite in
    # Tests hat kein astext) -> portabler Python-Vergleich statt DB-seitigem
    # JSON-Operator.
    tenant: Tenant | None = None
    for candidate in (await db.execute(select(Tenant))).scalars().all():
        stored = (candidate.settings or {}).get("ics_feed_token") or ""
        if stored and stored == token:
            tenant = candidate
            break
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Ungueltiger Token")
    ics = await _ics_events_for(db, tenant.id, None, mask_sick=True)
    return Response(content=ics, media_type="text/calendar; charset=utf-8")
