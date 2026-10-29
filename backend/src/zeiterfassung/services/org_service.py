"""Org-structure service: scanned NFC cards, admin employee view, departments,
companies, roles, login accounts, org chart.

Moved verbatim out of the former monolithic ``api/v1/users.py``
(zeiterfassung-6pcv, "Administration tool: full employee view + org
structure" section). Permission checks that need the resolved ``AuthUser``
object stay in the router; everything DB-query-backed lives here.
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from zeiterfassung.db.scoping import get_scoped, tenant_scoped

# ---------------------------------------------------------------------------
# Scanned-cards pool
# ---------------------------------------------------------------------------


async def list_scanned_cards(db: AsyncSession, tenant_id: uuid.UUID) -> list[dict]:
    """Pool of scanned unknown NFC cards, excluding cards already assigned to an
    active user (those are "used")."""
    from zeiterfassung.models.tenant import Tenant
    from zeiterfassung.models.user import User

    tenant = (await db.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one_or_none()
    cards = list((tenant.settings or {}).get("scanned_cards") or []) if tenant else []

    used = {
        r[0]
        for r in (
            await db.execute(
                select(User.nfc_badge_id).where(
                    User.tenant_id == tenant_id,
                    User.is_active.is_(True),
                    User.nfc_badge_id.isnot(None),
                )
            )
        ).all()
    }
    return [c for c in cards if c.get("uid") not in used]


async def remove_scanned_card(db: AsyncSession, tenant_id: uuid.UUID, uid: str | None) -> dict:
    """Remove a UID from the scanned-cards pool."""
    from zeiterfassung.models.tenant import Tenant

    tenant = (await db.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one_or_none()
    if tenant is not None:
        settings = dict(tenant.settings or {})
        settings["scanned_cards"] = [
            c for c in (settings.get("scanned_cards") or []) if c.get("uid") != uid
        ]
        tenant.settings = settings
        await db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Admin employee view
# ---------------------------------------------------------------------------


async def list_admin_employees(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    full_access: bool,
    caller_department_ids: list[uuid.UUID] | None,
    caller_user_id: uuid.UUID,
) -> list[dict]:
    """Full per-employee admin view (joins department, role, manager).

    ``full_access`` / ``caller_department_ids`` are resolved by the router
    (permission checks need the ``AuthUser`` object); the department-scope
    filter for Teamleiter and the actual query/join logic live here.
    """
    from zeiterfassung.models.account import Account, AccountRole
    from zeiterfassung.models.company import Company
    from zeiterfassung.models.department import Department
    from zeiterfassung.models.tenant import Tenant
    from zeiterfassung.models.user import Role, User, UserRole

    tid = tenant_id
    users_stmt = tenant_scoped(select(User), User, tid)
    if not full_access:
        # Teamleiter: nur der eigene Bereich (ohne Bereich -> nur sich selbst).
        if caller_department_ids:
            users_stmt = users_stmt.where(User.department_id.in_(caller_department_ids))
        else:
            users_stmt = users_stmt.where(User.id == caller_user_id)
    users = list((await db.execute(users_stmt)).scalars().all())
    tenant = (await db.execute(select(Tenant).where(Tenant.id == tid))).scalar_one_or_none()
    schedules = (tenant.settings or {}).get("work_schedules") or [] if tenant else []
    schedule_names = {str(s.get("id")): s.get("name", "") for s in schedules}
    # Handy-UID (chip_uid) zugewiesener Pool-Karten: liegt nach der Zuweisung in
    # assigned_card_meta (siehe user_service._sync_badge_pool); Fallback auf einen
    # Alt-Pool-Eintrag gleicher UID (Zuweisungen vor dem Pool-Konsistenz-Fix).
    tenant_settings = (tenant.settings or {}) if tenant else {}
    card_meta = tenant_settings.get("assigned_card_meta") or {}
    pool_chip = {
        c.get("uid"): c.get("chip_uid")
        for c in (tenant_settings.get("scanned_cards") or [])
        if c.get("chip_uid")
    }
    depts = {
        d.id: d.name
        for d in (await db.execute(tenant_scoped(select(Department), Department, tid)))
        .scalars()
        .all()
    }
    companies = {
        co.id: co.name
        for co in (await db.execute(tenant_scoped(select(Company), Company, tid))).scalars().all()
    }
    roles = {
        r.id: r.name
        for r in (await db.execute(tenant_scoped(select(Role), Role, tid))).scalars().all()
    }
    uroles: dict = {}
    for ur in (await db.execute(select(UserRole))).scalars().all():
        uroles.setdefault(ur.user_id, ur.role_id)
    # Rolle bevorzugt aus dem verknüpften Login-Account (account_roles); die alte
    # user_roles-Tabelle ist seit dem Accounts/Employees-Split meist leer.
    accounts = list(
        (
            await db.execute(
                tenant_scoped(select(Account), Account, tid).where(Account.employee_id.isnot(None))
            )
        )
        .scalars()
        .all()
    )
    acc_role: dict = {}
    acc_ids = [a.id for a in accounts]
    if acc_ids:
        for ar in (
            (await db.execute(select(AccountRole).where(AccountRole.account_id.in_(acc_ids))))
            .scalars()
            .all()
        ):
            acc_role.setdefault(ar.account_id, ar.role_id)
    emp_role: dict = {}
    for a in accounts:
        rid_a = acc_role.get(a.id)
        if rid_a is not None:
            emp_role.setdefault(a.employee_id, rid_a)
    names = {u.id: f"{u.first_name} {u.last_name}" for u in users}

    out = []
    for u in sorted(users, key=lambda x: ((x.last_name or ""), (x.first_name or ""))):
        rid = emp_role.get(u.id) or uroles.get(u.id)
        out.append(
            {
                "id": str(u.id),
                "name": f"{u.first_name} {u.last_name}",
                "first_name": u.first_name,
                "last_name": u.last_name,
                "email": u.email,
                "employee_number": u.employee_number,
                "nfc_badge_id": u.nfc_badge_id,
                "nfc_chip_uid": (
                    (card_meta.get(u.nfc_badge_id) or {}).get("chip_uid")
                    or pool_chip.get(u.nfc_badge_id)
                )
                if u.nfc_badge_id
                else None,
                "department_id": str(u.department_id) if u.department_id else None,
                "department_name": depts.get(u.department_id),
                "company_id": str(u.company_id) if u.company_id else None,
                "company_name": companies.get(u.company_id),
                "manager_id": str(u.manager_id) if u.manager_id else None,
                "manager_name": names.get(u.manager_id),
                "role_id": str(rid) if rid else None,
                "role_name": roles.get(rid),
                "time_capture": u.time_capture,
                "auto_hours_per_day": float(u.auto_hours_per_day),
                "employment_status": u.employment_status,
                "contracted_hours_per_week": float(u.contracted_hours_per_week),
                "work_schedule_id": u.work_schedule_id,
                "wage_profile_id": u.wage_profile_id,
                "work_time_profile_id": str(u.work_time_profile_id)
                if u.work_time_profile_id
                else None,
                "work_schedule_name": schedule_names.get(str(u.work_schedule_id), "")
                if u.work_schedule_id
                else "",
                "hire_date": u.hire_date.isoformat() if u.hire_date else None,
                "birth_date": u.birth_date.isoformat() if u.birth_date else None,
                "overtime_payout_mode": u.overtime_payout_mode,
                "overtime_cap_hours": float(u.overtime_cap_hours)
                if u.overtime_cap_hours is not None
                else None,
                "employment_type": u.employment_type,
                "vacation_entitlement_days": float(u.vacation_entitlement_days)
                if u.vacation_entitlement_days is not None
                else None,
                "is_active": u.is_active,
                "termination_date": u.termination_date.isoformat() if u.termination_date else None,
                "termination_reason": u.termination_reason,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Departments
# ---------------------------------------------------------------------------


async def create_department(db: AsyncSession, tenant_id: uuid.UUID, body: dict) -> dict:
    """Create a new department for the caller's tenant.

    Bereiche sind tenant-weit und NICHT an eine Firma gebunden (#183):
    die beiden Firmen sind operativ ein Betrieb, nur steuerlich getrennt —
    ein Bereich wie "Logistik" umfasst die Leute beider Firmen. ``company_id``
    im Body wird ignoriert; die Spalte bleibt aus Kompatibilitaet NULL.
    """
    from sqlalchemy.exc import IntegrityError

    from zeiterfassung.models.department import Department

    tid = tenant_id
    name = (body.get("name") or "").strip()
    code = (body.get("code") or "").strip()
    if not name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="name required")

    if not code:
        # Kürzel wird im Frontend nicht mehr gepflegt → aus dem Namen ableiten (eindeutig).
        base = "".join(ch for ch in name.upper() if ch.isalnum())[:8] or "DEPT"
        code = base
        n = 1
        while (
            await db.execute(
                tenant_scoped(select(Department), Department, tid).where(Department.code == code)
            )
        ).scalar_one_or_none() is not None:
            n += 1
            code = f"{base[:6]}{n}"

    existing = (
        await db.execute(
            tenant_scoped(select(Department), Department, tid).where(Department.code == code)
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=f"Department code {code!r} already exists"
        )

    dept = Department(tenant_id=tid, name=name, code=code, company_id=None, is_active=True)
    db.add(dept)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=f"Department code {code!r} already exists"
        ) from exc
    return {"ok": True, "id": str(dept.id)}


async def delete_department(db: AsyncSession, tenant_id: uuid.UUID, department_id: str) -> None:
    """Delete a department. Detaches employees first (department_id → null)."""
    import uuid as _uuid

    from sqlalchemy import update

    from zeiterfassung.models.department import Department
    from zeiterfassung.models.user import User

    tid = tenant_id
    try:
        did = _uuid.UUID(str(department_id))
    except ValueError as err:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Department not found"
        ) from err

    dept = await get_scoped(db, Department, tid, did)
    if dept is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Department not found")

    # Zugeordnete Mitarbeiter lösen, dann Bereich löschen.
    await db.execute(
        update(User)
        .where(User.tenant_id == tid, User.department_id == did)
        .values(department_id=None)
    )
    await db.delete(dept)
    await db.commit()


async def list_departments(
    db: AsyncSession, tenant_id: uuid.UUID, company_id: uuid.UUID | None
) -> list[dict]:
    """List departments (incl. team lead) for the admin dropdowns.

    ``company_id`` ist deprecated und wird ignoriert: Bereiche gelten
    tenant-weit fuer alle Firmen (#183). Frueher filterte der Parameter
    firmenlose Bereiche still weg.
    """
    from zeiterfassung.models.company import Company
    from zeiterfassung.models.department import Department
    from zeiterfassung.models.user import User

    _ = company_id  # deprecated, siehe Docstring
    tid = tenant_id
    depts = list(
        (await db.execute(tenant_scoped(select(Department), Department, tid))).scalars().all()
    )
    names = {
        u.id: f"{u.first_name} {u.last_name}"
        for u in (await db.execute(tenant_scoped(select(User), User, tid))).scalars().all()
    }
    company_names = {
        c.id: c.name
        for c in (await db.execute(tenant_scoped(select(Company), Company, tid))).scalars().all()
    }
    return [
        {
            "id": str(d.id),
            "name": d.name,
            "code": d.code,
            "teamleiter_user_id": str(d.teamleiter_user_id) if d.teamleiter_user_id else None,
            "teamleiter_name": names.get(d.teamleiter_user_id),
            "is_active": d.is_active,
            "company_id": str(d.company_id) if d.company_id else None,
            "company_name": company_names.get(d.company_id),
        }
        for d in sorted(depts, key=lambda x: x.name)
    ]


async def set_department_lead(
    db: AsyncSession, tenant_id: uuid.UUID, department_id: uuid.UUID, body: dict
) -> dict:
    """Update a department: team lead, name and/or code.

    Body fields are all optional: ``name``, ``code``, ``teamleiter_user_id``.
    """
    from sqlalchemy.exc import IntegrityError

    from zeiterfassung.models.department import Department

    dept = await get_scoped(db, Department, tenant_id, department_id)
    if dept is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Department not found")

    # ``company_id`` wird bewusst ignoriert: Bereiche sind tenant-weit und
    # nicht an eine Firma gebunden (#183).

    if "teamleiter_user_id" in body:
        raw = body.get("teamleiter_user_id")
        dept.teamleiter_user_id = uuid.UUID(raw) if raw else None
    if "name" in body and body.get("name") is not None:
        name = str(body["name"]).strip()
        if name:
            dept.name = name
    if "code" in body and body.get("code") is not None:
        code = str(body["code"]).strip()
        if code and code != dept.code:
            clash = (
                await db.execute(
                    tenant_scoped(select(Department), Department, tenant_id).where(
                        Department.code == code,
                        Department.id != dept.id,
                    )
                )
            ).scalar_one_or_none()
            if clash is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Department code {code!r} already exists",
                )
            dept.code = code

    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Department code already exists"
        ) from exc
    return {"ok": True}


# ---------------------------------------------------------------------------
# Companies (Firmen) — employee grouping, NOT tenants
# ---------------------------------------------------------------------------


async def list_companies(db: AsyncSession, tenant_id: uuid.UUID) -> list[dict]:
    """List companies for the tenant."""
    from zeiterfassung.models.company import Company

    rows = list(
        (await db.execute(tenant_scoped(select(Company), Company, tenant_id))).scalars().all()
    )
    rows.sort(key=lambda c: (c.name or "").lower())
    return [
        {
            "id": str(c.id),
            "name": c.name,
            "is_active": c.is_active,
            "advisor_number": c.advisor_number,
            "client_number": c.client_number,
        }
        for c in rows
    ]


async def create_company(db: AsyncSession, tenant_id: uuid.UUID, body: dict) -> dict:
    """Create a company."""
    from zeiterfassung.models.company import Company

    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="name required")
    co = Company(
        tenant_id=tenant_id,
        name=name,
        is_active=True,
        advisor_number=(str(body.get("advisor_number")).strip() or None)
        if body.get("advisor_number")
        else None,
        client_number=(str(body.get("client_number")).strip() or None)
        if body.get("client_number")
        else None,
    )
    db.add(co)
    await db.commit()
    return {"ok": True, "id": str(co.id)}


async def update_company(
    db: AsyncSession, tenant_id: uuid.UUID, company_id: uuid.UUID, body: dict
) -> dict:
    """Rename a company."""
    from zeiterfassung.models.company import Company

    co = await get_scoped(db, Company, tenant_id, company_id)
    if co is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found")
    if body.get("name") is not None and str(body["name"]).strip():
        co.name = str(body["name"]).strip()
    if "advisor_number" in body:
        v = str(body.get("advisor_number") or "").strip()
        co.advisor_number = v or None
    if "client_number" in body:
        v = str(body.get("client_number") or "").strip()
        co.client_number = v or None
    await db.commit()
    return {"ok": True}


async def delete_company(db: AsyncSession, tenant_id: uuid.UUID, company_id: uuid.UUID) -> None:
    """Delete a company. Employees keep existing — their company_id is set NULL via FK."""
    from zeiterfassung.models.company import Company

    co = await get_scoped(db, Company, tenant_id, company_id)
    if co is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found")
    await db.delete(co)
    await db.commit()


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------


async def create_role(db: AsyncSession, tenant_id: uuid.UUID, body: dict) -> dict:
    """Create a new role for the caller's tenant."""
    from zeiterfassung.models.user import Role

    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="name required")
    role_type = body.get("role_type") or None
    data_scope = (body.get("data_scope") or "").strip() or "self"
    permissions = body.get("permissions")
    if permissions is None:
        permissions = []

    role = Role(
        tenant_id=tenant_id,
        name=name,
        role_type=role_type,
        data_scope=data_scope,
        permissions=list(permissions),
    )
    db.add(role)
    await db.commit()
    return {"ok": True, "id": str(role.id)}


async def list_roles(db: AsyncSession, tenant_id: uuid.UUID) -> list[dict]:
    """List roles (status levels) for the admin dropdowns."""
    from zeiterfassung.models.user import Role

    roles = list((await db.execute(tenant_scoped(select(Role), Role, tenant_id))).scalars().all())
    return [
        {
            "id": str(r.id),
            "name": r.name,
            "role_type": r.role_type,
            "data_scope": r.data_scope,
            "permissions": list(r.permissions or []),
        }
        for r in sorted(roles, key=lambda x: x.name)
    ]


async def update_role(
    db: AsyncSession, tenant_id: uuid.UUID, role_id: uuid.UUID, body: dict
) -> dict:
    """Update a role. Body fields all optional: name, role_type, data_scope, permissions."""
    from zeiterfassung.models.user import Role

    role = await get_scoped(db, Role, tenant_id, role_id)
    if role is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Role not found")

    if "name" in body and body.get("name") is not None:
        name = str(body["name"]).strip()
        if name:
            role.name = name
    if "role_type" in body:
        role.role_type = body.get("role_type") or None
    if "data_scope" in body and body.get("data_scope"):
        role.data_scope = str(body["data_scope"]).strip()
    if "permissions" in body and body.get("permissions") is not None:
        role.permissions = list(body["permissions"])

    await db.commit()
    return {"ok": True}


async def delete_role(db: AsyncSession, tenant_id: uuid.UUID, role_id: uuid.UUID) -> None:
    """Delete a role. Account/employee assignments cascade away."""
    from zeiterfassung.models.user import Role

    role = await get_scoped(db, Role, tenant_id, role_id)
    if role is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Role not found")
    await db.delete(role)
    await db.commit()


# ---------------------------------------------------------------------------
# Accounts (login identities, separate from employees)
# ---------------------------------------------------------------------------


async def validate_scope_departments(
    db: AsyncSession, tenant_id: uuid.UUID, raw: list | None
) -> list[str] | None:
    """Zustaendige Bereiche pruefen: alle IDs muessen Departments des eigenen
    Mandanten sein. Leere Liste/None -> None (= Personalakte zaehlt)."""
    if not raw:
        return None
    from zeiterfassung.models.department import Department

    ids: list[uuid.UUID] = []
    for x in raw:
        try:
            ids.append(uuid.UUID(str(x)))
        except (ValueError, AttributeError):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Ungueltige Bereichs-ID: {x}",
            ) from None
    found = {
        r[0]
        for r in (
            await db.execute(
                select(Department.id).where(
                    Department.tenant_id == tenant_id, Department.id.in_(ids)
                )
            )
        ).all()
    }
    missing = [str(i) for i in ids if i not in found]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Bereich nicht gefunden: {', '.join(missing)}",
        )
    return [str(i) for i in ids]


async def list_accounts(db: AsyncSession, tenant_id: uuid.UUID) -> list[dict]:
    """List login accounts for the tenant."""
    from zeiterfassung.models.account import Account, AccountRole
    from zeiterfassung.models.user import Role, User

    tid = tenant_id
    accounts = list(
        (
            await db.execute(
                select(Account).where((Account.tenant_id == tid) | (Account.tenant_id.is_(None)))
            )
        )
        .scalars()
        .all()
    )
    out: list[dict] = []
    for a in accounts:
        role = (
            (
                await db.execute(
                    select(Role)
                    .join(AccountRole, Role.id == AccountRole.role_id)
                    .where(AccountRole.account_id == a.id)
                )
            )
            .scalars()
            .first()
        )
        emp_name = None
        if a.employee_id:
            emp = (
                await db.execute(select(User).where(User.id == a.employee_id))
            ).scalar_one_or_none()
            if emp is not None:
                emp_name = emp.full_name
        out.append(
            {
                "id": str(a.id),
                "email": a.email,
                "is_superadmin": a.is_superadmin,
                "is_active": a.is_active,
                "employee_id": str(a.employee_id) if a.employee_id else None,
                "employee_name": emp_name,
                "role_id": str(role.id) if role else None,
                "role_name": role.name if role else None,
                "scope_department_ids": list(a.scope_department_ids or []),
            }
        )
    return out


async def create_account(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    body: dict,
    *,
    actor_is_superadmin: bool,
    actor_can_manage_roles: bool,
) -> dict:
    """Create a login account.

    Body: email, password, role_id, optional employee_id/is_superadmin.
    """
    from zeiterfassung.core.security import hash_password
    from zeiterfassung.models.account import Account, AccountRole

    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""
    if not email or len(password) < 8:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="email and password (min 8) required"
        )
    if (
        await db.execute(select(Account).where(Account.email == email))
    ).scalar_one_or_none() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="email already exists")

    # is_superadmin darf NUR ein Superadmin vergeben (sonst Privilegien-Eskalation).
    want_superadmin = bool(body.get("is_superadmin", False))
    if want_superadmin and not actor_is_superadmin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Nur ein Superadmin darf Superadmin-Konten anlegen.",
        )
    emp_id = body.get("employee_id")
    if emp_id:
        # Verknuepfter Mitarbeiter muss zum eigenen Mandanten gehoeren.
        from zeiterfassung.models.user import User as _User

        ok = (
            await db.execute(
                select(_User.id).where(
                    _User.id == uuid.UUID(str(emp_id)),
                    _User.tenant_id == tenant_id,
                )
            )
        ).first()
        if ok is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Mitarbeiter nicht gefunden"
            )
    scope_dept_ids = await validate_scope_departments(
        db, tenant_id, body.get("scope_department_ids")
    )
    # Rollen zuweisen ist ein eigenstaendiges Recht (admin:roles) — sonst koennte
    # ein Accounts-Admin (nur admin:accounts) sich/anderen ueber role_id beliebige
    # Rechte verschaffen (Privilegien-Eskalation).
    role_uuid: uuid.UUID | None = None
    if body.get("role_id"):
        if not actor_can_manage_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="admin:roles permission required, um eine Rolle zuzuweisen.",
            )
        from zeiterfassung.models.user import Role as _Role

        try:
            role_uuid = uuid.UUID(str(body["role_id"]))
        except (ValueError, AttributeError):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Ungueltige Rollen-ID"
            ) from None
        role_ok = (
            await db.execute(
                select(_Role.id).where(_Role.id == role_uuid, _Role.tenant_id == tenant_id)
            )
        ).first()
        if role_ok is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Rolle nicht gefunden"
            )
    acc = Account(
        tenant_id=tenant_id,
        email=email,
        hashed_password=hash_password(password),
        is_superadmin=want_superadmin,
        employee_id=uuid.UUID(emp_id) if emp_id else None,
        scope_department_ids=scope_dept_ids,
        is_active=True,
    )
    db.add(acc)
    await db.flush()
    if role_uuid is not None:
        db.add(AccountRole(account_id=acc.id, role_id=role_uuid))
    await db.commit()
    return {"ok": True, "id": str(acc.id)}


async def update_account(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    body: dict,
    *,
    actor_account_id: uuid.UUID | None,
    actor_is_superadmin: bool,
    actor_can_manage_roles: bool,
) -> dict:
    """Update an account: role_id, is_active, employee_id, is_superadmin, password."""
    from sqlalchemy import delete as sa_delete

    from zeiterfassung.core.security import hash_password
    from zeiterfassung.models.account import Account, AccountRole

    acc = await get_scoped(db, Account, tenant_id, account_id)  # Tenant-Isolation
    if acc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")

    if body.get("is_active") is not None:
        acc.is_active = bool(body["is_active"])
    if "employment_status" in body:  # Komfort: "deleted" -> inaktiv
        acc.is_active = body["employment_status"] == "active"
    if body.get("is_superadmin") is not None:
        # Superadmin-Flag darf nur ein Superadmin vergeben/entziehen.
        if not actor_is_superadmin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Nur ein Superadmin darf das Superadmin-Flag aendern.",
            )
        acc.is_superadmin = bool(body["is_superadmin"])
    if "employee_id" in body:
        if body["employee_id"]:
            from zeiterfassung.models.user import User as _User

            ok = (
                await db.execute(
                    select(_User.id).where(
                        _User.id == uuid.UUID(body["employee_id"]),
                        _User.tenant_id == tenant_id,
                    )
                )
            ).first()
            if ok is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="Mitarbeiter nicht gefunden"
                )
        acc.employee_id = uuid.UUID(body["employee_id"]) if body["employee_id"] else None
    if "scope_department_ids" in body:
        acc.scope_department_ids = await validate_scope_departments(
            db, tenant_id, body.get("scope_department_ids")
        )
    if body.get("password"):
        acc.hashed_password = hash_password(body["password"])
    if "role_id" in body:
        # Eigene Rolle nicht selbst aenderbar (mirror des Selbst-Loesch-Schutzes
        # in delete_account) — sonst koennte man sich z.B. eine Rolle mit mehr
        # Rechten selbst zuweisen oder die eigene Rolle folgenlos entziehen.
        if actor_account_id is not None and acc.id == actor_account_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Die eigene Rolle kann nicht geaendert werden.",
            )
        # Rollen zuweisen/entziehen ist ein eigenstaendiges Recht (admin:roles) —
        # sonst koennte ein Accounts-Admin (nur admin:accounts) sich/anderen
        # ueber role_id beliebige Rechte verschaffen (Privilegien-Eskalation).
        if not actor_can_manage_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="admin:roles permission required, um eine Rolle zu aendern.",
            )
        role_uuid: uuid.UUID | None = None
        if body["role_id"]:
            from zeiterfassung.models.user import Role as _Role

            try:
                role_uuid = uuid.UUID(str(body["role_id"]))
            except (ValueError, AttributeError):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Ungueltige Rollen-ID"
                ) from None
            role_ok = (
                await db.execute(
                    select(_Role.id).where(_Role.id == role_uuid, _Role.tenant_id == tenant_id)
                )
            ).first()
            if role_ok is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="Rolle nicht gefunden"
                )
        await db.execute(sa_delete(AccountRole).where(AccountRole.account_id == acc.id))
        if role_uuid is not None:
            db.add(AccountRole(account_id=acc.id, role_id=role_uuid))
    await db.commit()
    return {"ok": True}


def _generate_start_password() -> str:
    """Zufälliges Start-Passwort nach einem Admin-Reset (Nutzer ändert es danach selbst)."""
    import secrets

    return secrets.token_urlsafe(9)


async def reset_account_password(
    db: AsyncSession, tenant_id: uuid.UUID, account_id: uuid.UUID, *, actor_is_superadmin: bool
) -> dict:
    """Admin setzt das Passwort eines Accounts auf das Start-Passwort zurück.

    Gibt das Start-Passwort zurück, damit der Admin es weitergeben kann.
    """
    from zeiterfassung.core.security import hash_password
    from zeiterfassung.models.account import Account

    acc = await get_scoped(db, Account, tenant_id, account_id)  # Tenant-Isolation
    if acc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")
    if acc.is_superadmin and not actor_is_superadmin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Nur ein Superadmin darf Superadmin-Passwoerter zuruecksetzen.",
        )
    start_password = _generate_start_password()
    acc.hashed_password = hash_password(start_password)
    from zeiterfassung.services.user_service import invalidate_account_tokens

    await invalidate_account_tokens(db, acc)
    await db.commit()
    return {"ok": True, "password": start_password}


async def delete_account(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    *,
    actor_account_id: uuid.UUID | None,
    actor_employee_id: uuid.UUID | None,
) -> None:
    """Login-Konto endgueltig loeschen. Der verknuepfte Mitarbeiter und seine
    Zeitdaten bleiben erhalten (Login != Mitarbeiter).

    Sperren: das eigene Konto sowie der letzte aktive Superadmin sind nicht
    loeschbar.
    """
    from sqlalchemy import func

    from zeiterfassung.models.account import Account
    from zeiterfassung.services import audit_service

    acc = await get_scoped(db, Account, tenant_id, account_id)
    if acc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")
    if actor_account_id is not None and acc.id == actor_account_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Das eigene Konto kann nicht geloescht werden.",
        )
    if acc.is_superadmin and acc.is_active:
        others = (
            await db.execute(
                select(func.count())
                .select_from(Account)
                .where(
                    Account.tenant_id == tenant_id,
                    Account.is_superadmin == True,  # noqa: E712
                    Account.is_active == True,  # noqa: E712
                    Account.id != acc.id,
                )
            )
        ).scalar_one()
        if others == 0:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Der letzte aktive Superadmin kann nicht geloescht werden.",
            )
    email = acc.email
    was_superadmin = acc.is_superadmin
    await db.delete(acc)  # AccountRole cascadet; users.employee_id -> SET NULL
    await audit_service.log(
        db,
        tenant_id=tenant_id,
        actor_id=actor_employee_id,
        action="account.delete",
        resource_type="account",
        resource_id=str(account_id),
        details={"email": email, "was_superadmin": was_superadmin},
    )
    await db.commit()


# ---------------------------------------------------------------------------
# Org chart
# ---------------------------------------------------------------------------


async def org_chart(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    view_all: bool,
    view_team: bool,
    caller_department_ids: list[uuid.UUID] | None,
    caller_user_id: uuid.UUID,
) -> dict:
    """Schlankes Organigramm: Name, Bereich, Vorgesetzter — sonst nichts.

    ``view_all``/``view_team`` are resolved by the router (need the
    ``AuthUser`` permission checks).
    """
    from zeiterfassung.models.department import Department
    from zeiterfassung.models.user import User

    if not view_all and not view_team:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="orgchart:view_team oder orgchart:view_all erforderlich",
        )

    # Funktions-Schalter: Organigramm kann in den Einstellungen deaktiviert sein.
    from zeiterfassung.models.tenant import Tenant

    tenant = (await db.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one_or_none()
    features = ((tenant.settings or {}).get("features") or {}) if tenant else {}
    if features.get("org_chart", True) is False:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Das Organigramm ist in den Einstellungen deaktiviert.",
        )

    stmt = tenant_scoped(select(User), User, tenant_id).where(
        User.is_active == True,  # noqa: E712
    )
    if not view_all:
        if caller_department_ids:
            stmt = stmt.where(User.department_id.in_(caller_department_ids))
        else:
            stmt = stmt.where(User.id == caller_user_id)
    employees = list((await db.execute(stmt)).scalars().all())

    dept_names = {
        d.id: d.name
        for d in (await db.execute(tenant_scoped(select(Department), Department, tenant_id)))
        .scalars()
        .all()
    }

    return {
        "employees": [
            {
                "id": str(u.id),
                "name": f"{u.first_name} {u.last_name}",
                "department_name": dept_names.get(u.department_id),
                "manager_id": str(u.manager_id) if u.manager_id else None,
            }
            for u in sorted(employees, key=lambda u: (u.last_name, u.first_name))
        ],
    }
