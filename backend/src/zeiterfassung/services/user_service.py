"""User and authentication service."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from zeiterfassung.api.deps import CurrentUser, ScopeContext
    from zeiterfassung.models.account import Account

from zeiterfassung.core.errors import DomainError, ErrorCode
from zeiterfassung.core.security import (
    create_access_token,
    create_refresh_token,
    decode_refresh_token,
    hash_password,
    verify_password,
)
from zeiterfassung.db.scoping import get_scoped, tenant_scoped
from zeiterfassung.models.user import User, UserRole
from zeiterfassung.schemas.user import UserCreate, UserUpdate
from zeiterfassung.services import audit_service

# Login-Lockout (fkw.4, DB-basiert, kein Redis — User-Entscheid 2026-07-17):
# 10 Fehlversuche in Folge -> 5 min Sperre. Siehe authenticate().
LOGIN_LOCKOUT_THRESHOLD = 10
LOGIN_LOCKOUT_DURATION = timedelta(minutes=5)


@dataclass(frozen=True)
class TokenPair:
    """Internal (service-layer) token result — includes the raw refresh token.

    ``TokenResponse`` (the public API schema) deliberately does NOT carry the
    refresh token (fkw.4, cookie-only transition end) — the router extracts
    ``refresh_token`` here to set the httpOnly cookie, then builds the public
    ``TokenResponse`` from ``access_token``/``expires_in`` only.
    """

    access_token: str
    refresh_token: str
    expires_in: int


async def _sync_badge_pool(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    assigned: str | None = None,
    freed: str | None = None,
) -> None:
    """Karten-Pool (tenant.settings.scanned_cards) mit Badge-Zuweisungen synchron halten.

    - ``assigned``: Karte wird einem Mitarbeiter zugewiesen -> Pool-Eintrag entfernen,
      seine Metadaten (chip_uid) aber unter ``assigned_card_meta`` aufheben.
    - ``freed``: Karte wird frei (Entzug/Umhaengen/Archivierung) -> nur wenn sie
      urspruenglich aus dem Pool kam (Metadaten vorhanden), zurueck in den Pool —
      manuell vergebene Alt-Badges (z.B. "NFC005") landen nicht im Pool.
    """
    if assigned is None and freed is None:
        return
    from zeiterfassung.models.tenant import Tenant

    tenant = (await db.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one_or_none()
    if tenant is None:
        return
    settings = dict(tenant.settings or {})
    cards = [dict(c) for c in (settings.get("scanned_cards") or [])]
    meta = dict(settings.get("assigned_card_meta") or {})
    changed = False

    if freed:
        entry_meta = meta.pop(freed, None)
        if entry_meta is not None:
            changed = True
            if not any(c.get("uid") == freed for c in cards):
                entry = {"uid": freed}
                entry.update({k: v for k, v in entry_meta.items() if k != "uid"})
                entry["at"] = datetime.now(tz=UTC).isoformat()
                cards.append(entry)

    if assigned:
        remaining = [c for c in cards if c.get("uid") != assigned]
        if len(remaining) != len(cards):
            entry = next(c for c in cards if c.get("uid") == assigned)
            meta[assigned] = {k: v for k, v in entry.items() if k != "uid"}
            cards = remaining
            changed = True

    if changed:
        settings["scanned_cards"] = cards
        settings["assigned_card_meta"] = meta
        tenant.settings = settings
        await db.flush()


class UserNotFoundError(Exception):
    pass


class InvalidCredentialsError(Exception):
    pass


class DuplicateEmailError(DomainError):
    code = ErrorCode.DUPLICATE_EMAIL


class DepartmentCompanyMismatchError(DomainError):
    code = ErrorCode.DEPARTMENT_COMPANY_MISMATCH


class RoleNotFoundError(DomainError):
    code = ErrorCode.ROLE_NOT_FOUND


class WorkTimeProfileMismatchError(DomainError):
    code = ErrorCode.WORK_TIME_PROFILE_MISMATCH


async def _check_department_company_consistency(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    department_id: uuid.UUID | None,
    company_id: uuid.UUID | None,
) -> None:
    """Bereich und Firma muessen zum eigenen Mandanten gehoeren (#100:
    department_id/company_id waren bisher nicht Mandanten-geprueft).

    Eine Firmen-Zuordnung des Bereichs wird NICHT mehr geprueft: Bereiche sind
    tenant-weit und firmenunabhaengig (#183) — jeder Bereich passt zu
    jedem Mitarbeiter, egal in welcher Firma.
    """
    from zeiterfassung.models.company import Company
    from zeiterfassung.models.department import Department

    if department_id is not None:
        dept = await get_scoped(db, Department, tenant_id, department_id)
        if dept is None:
            raise DepartmentCompanyMismatchError("Bereich nicht gefunden")

    if company_id is not None:
        company_ok = (
            await db.execute(
                tenant_scoped(select(Company.id), Company, tenant_id).where(
                    Company.id == company_id
                )
            )
        ).first()
        if company_ok is None:
            raise DepartmentCompanyMismatchError("Firma nicht gefunden")


async def _validate_role_tenant(
    db: AsyncSession, tenant_id: uuid.UUID, role_id: uuid.UUID | None
) -> None:
    """Die zugewiesene Rolle muss zum Mandanten des Users gehoeren — sonst liesse
    sich per role_id eine Rolle eines fremden Mandanten (mit unbekannten
    Rechten) zuweisen."""
    if role_id is None:
        return
    from zeiterfassung.models.user import Role

    role_ok = (
        await db.execute(tenant_scoped(select(Role.id), Role, tenant_id).where(Role.id == role_id))
    ).first()
    if role_ok is None:
        raise RoleNotFoundError(f"Role {role_id} not found")


async def _check_work_time_profile_binding(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    user: User,
    *,
    profile_explicitly_set: bool,
) -> None:
    """Typ- und Stunden-Bindung Arbeitszeit-Profil <-> User (Spec 2026-08-14).

    kind 'nfc' passt nur zu ``time_capture in ('nfc', 'web')``, kind 'auto'
    nur zu ``time_capture == 'auto'``. Nachtrag 14.08.: hat das Profil
    Vertragsstunden (nfc: gespeichert; auto: aus ``weekday_times`` BERECHNET),
    muessen sie exakt ``user.contracted_hours_per_week`` entsprechen; Profile
    ohne Stunden (Bestand bzw. auto ohne ``weekday_times``) binden nicht.
    Wird serverseitig bei Profil-Zuweisung, beim Wechsel des Erfassungsmodus
    UND beim Aendern der Vertragsstunden geprueft — das Formular ist Komfort,
    kein Schutz (.claude/rules/ui-limits-serverseitig-spiegeln.md).
    """
    if user.work_time_profile_id is None:
        return
    from zeiterfassung.models.work_time_profile import WorkTimeProfile
    from zeiterfassung.policy.worktime import auto_contracted_hours_per_week, fmt_hours

    profile = await get_scoped(db, WorkTimeProfile, tenant_id, user.work_time_profile_id)
    if profile is None:
        raise WorkTimeProfileMismatchError("Arbeitszeit-Profil nicht gefunden")

    kind = str(profile.kind)
    compatible = (
        user.time_capture in ("nfc", "web") if kind == "nfc" else user.time_capture == "auto"
    )
    if not compatible:
        if profile_explicitly_set:
            raise WorkTimeProfileMismatchError(
                f"Arbeitszeit-Profil {profile.name!r} (Typ '{kind}') passt nicht zum "
                f"Erfassungsmodus '{user.time_capture}'."
            )
        raise WorkTimeProfileMismatchError(
            f"Erfassungsmodus '{user.time_capture}' passt nicht zum zugewiesenen "
            f"Arbeitszeit-Profil {profile.name!r} (Typ '{kind}') — erst das "
            "Arbeitszeit-Profil entfernen."
        )

    # Stunden-Bindung (Nachtrag 14.08.). Decimal-Vergleich: 38.5 == 38,50.
    raw_hours = (
        auto_contracted_hours_per_week(profile.weekday_times)
        if kind == "auto"
        else profile.contracted_hours_per_week
    )
    if raw_hours is None:
        return
    profile_hours = Decimal(str(raw_hours))
    user_hours = Decimal(str(user.contracted_hours_per_week or 0))
    if profile_hours == user_hours:
        return
    px, ux = fmt_hours(profile_hours), fmt_hours(user_hours)
    if profile_explicitly_set:
        raise WorkTimeProfileMismatchError(
            f"Arbeitszeit-Profil {profile.name!r} ist für {px} h/Woche ausgelegt, "
            f"Mitarbeiter hat {ux} h."
        )
    raise WorkTimeProfileMismatchError(
        f"Das zugewiesene Arbeitszeit-Profil {profile.name!r} ist für {px} h/Woche "
        f"ausgelegt, Mitarbeiter hätte {ux} h — erst das Arbeitszeit-Profil entfernen."
    )


async def get_user_by_id(db: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID) -> User:
    user = await get_scoped(db, User, tenant_id, user_id)
    if user is None:
        raise UserNotFoundError(f"User {user_id} not found")
    return user


async def get_user_by_email(db: AsyncSession, tenant_id: uuid.UUID, email: str) -> User | None:
    stmt = tenant_scoped(select(User), User, tenant_id).where(User.email == email.lower())
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def list_users(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    skip: int = 0,
    limit: int = 100,
    active_only: bool = True,
    scope: ScopeContext | None = None,
) -> list[User]:
    """Return users filtered by scope.

    - ``scope.is_global``: all tenant users (no additional employee filter)
    - ``scope.is_department``: users in the caller's department
    - ``scope.is_self_only``: only the caller themselves
    - ``scope.is_anonymized``: empty list (Betriebsrat cannot enumerate users)
    - ``scope=None``: legacy path — returns all tenant users (used by admin endpoints)
    """

    if scope is not None and scope.is_anonymized:
        return []

    stmt = tenant_scoped(select(User), User, tenant_id)
    if active_only:
        stmt = stmt.where(User.is_active == True)  # noqa: E712

    if scope is not None:
        if scope.is_self_only:
            stmt = stmt.where(User.id == scope.user_id)
        elif scope.is_department:
            if not scope.dept_ids:
                stmt = stmt.where(User.id == scope.user_id)
            else:
                stmt = stmt.where(User.department_id.in_(scope.dept_ids))
        # is_global: no additional filter

    stmt = stmt.offset(skip).limit(limit).order_by(User.last_name, User.first_name)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def create_user(
    db: AsyncSession, tenant_id: uuid.UUID, data: UserCreate, actor_id: uuid.UUID | None = None
) -> User:
    # E-Mail ist optional (nicht jeder Gewerbliche hat eine Firmenadresse).
    # Ohne Adresse entfaellt die Dublettenpruefung — leer kollidiert nie.
    if data.email:
        existing = await get_user_by_email(db, tenant_id, data.email)
        if existing is not None:
            raise DuplicateEmailError(f"Email {data.email!r} already registered")

    await _check_department_company_consistency(db, tenant_id, data.department_id, data.company_id)
    await _validate_role_tenant(db, tenant_id, data.role_id)

    vacation_entitlement_days = data.vacation_entitlement_days
    if vacation_entitlement_days is None:
        # Kein expliziter Wert -> Mandanten-Default (7tu: war vorher fix 30.0,
        # unabhaengig vom mandantenweiten Default). Siehe
        # absence_service.resolve_default_vacation_entitlement_days.
        from zeiterfassung.models.tenant import Tenant
        from zeiterfassung.services.absence_service import (
            resolve_default_vacation_entitlement_days,
        )

        tenant = await db.get(Tenant, tenant_id)
        vacation_entitlement_days = resolve_default_vacation_entitlement_days(tenant)

    now = datetime.now(tz=UTC)
    user = User(
        tenant_id=tenant_id,
        email=data.email.lower() if data.email else None,
        # Kein Passwort noetig: users dient als Personalakte, Login geht ueber
        # accounts. Ohne Passwort wird ein zufaelliger, nie eingebbarer Wert
        # gehasht (Spalte ist NOT NULL).
        hashed_password=hash_password(data.password or f"!disabled-{uuid.uuid4()}"),
        first_name=data.first_name,
        last_name=data.last_name,
        employee_number=data.employee_number,
        nfc_badge_id=data.nfc_badge_id,
        contracted_hours_per_week=data.contracted_hours_per_week,
        weekday_target_minutes=data.weekday_target_minutes,
        department_id=data.department_id,
        company_id=data.company_id,
        manager_id=data.manager_id,
        time_capture=data.time_capture,
        auto_hours_per_day=data.auto_hours_per_day,
        work_schedule_id=data.work_schedule_id,
        wage_profile_id=data.wage_profile_id,
        hire_date=data.hire_date,
        counting_start_date=data.counting_start_date,
        termination_date=data.termination_date,
        termination_reason=data.termination_reason,
        birth_date=data.birth_date,
        employment_type=data.employment_type,
        vacation_entitlement_days=vacation_entitlement_days,
        initial_vacation_remaining_days=data.initial_vacation_remaining_days,
        overtime_payout_mode=data.overtime_payout_mode,
        overtime_cap_hours=data.overtime_cap_hours,
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    db.add(user)
    await db.flush()
    if data.role_id is not None:
        db.add(UserRole(user_id=user.id, role_id=data.role_id))
        await db.flush()

    await audit_service.log(
        db,
        tenant_id=tenant_id,
        actor_id=actor_id,
        action="user.create",
        resource_type="user",
        resource_id=user.id,
        details={"email": user.email},
    )
    return user


async def update_user(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    data: UserUpdate,
    actor_id: uuid.UUID | None = None,
) -> User:
    user = await get_user_by_id(db, tenant_id, user_id)
    update_data = data.model_dump(exclude_unset=True)
    old_badge = user.nfc_badge_id

    role_id = update_data.pop("role_id", "__unset__")
    if role_id != "__unset__":
        await _validate_role_tenant(db, tenant_id, role_id)
        await db.execute(delete(UserRole).where(UserRole.user_id == user.id))
        if role_id is not None:
            db.add(UserRole(user_id=user.id, role_id=role_id))

    for field, value in update_data.items():
        if field == "email" and value is not None:
            value = value.lower()
        setattr(user, field, value)

    if "department_id" in update_data or "company_id" in update_data:
        await _check_department_company_consistency(
            db, tenant_id, user.department_id, user.company_id
        )

    if (
        "work_time_profile_id" in update_data
        or "time_capture" in update_data
        # Nachtrag 14.08.: Vertragsstunden-Aenderung bricht sonst still die
        # Stunden-Bindung eines zugewiesenen Profils.
        or "contracted_hours_per_week" in update_data
    ):
        await _check_work_time_profile_binding(
            db,
            tenant_id,
            user,
            profile_explicitly_set="work_time_profile_id" in update_data,
        )

    if "employment_status" in update_data:
        user.is_active = update_data["employment_status"] == "active"

    # Pool-Konsistenz (qei): eine zugewiesene Karte darf nicht gleichzeitig im
    # scanned_cards-Pool als "frei" stehen; eine entzogene Pool-Karte kommt zurueck.
    if "nfc_badge_id" in update_data and update_data["nfc_badge_id"] != old_badge:
        await _sync_badge_pool(db, tenant_id, assigned=update_data["nfc_badge_id"], freed=old_badge)

    user.updated_at = datetime.now(tz=UTC)
    await db.flush()

    await audit_service.log(
        db,
        tenant_id=tenant_id,
        actor_id=actor_id,
        action="user.update",
        resource_type="user",
        resource_id=user.id,
        details={"fields": sorted(update_data.keys())},
    )

    # Austritts-Automatik: Austrittsdatum in der Vergangenheit -> sofort
    # archivieren; Austrittsdatum entfernt -> Wiedereintritt (dearchivieren).
    if "termination_date" in update_data:
        from datetime import date as _date

        if user.termination_date and user.termination_date < _date.today() and user.is_active:
            await archive_user(db, user, actor_id=actor_id)
        elif user.termination_date is None and user.employment_status == "archived":
            await unarchive_user(db, user, actor_id=actor_id)

    return user


async def archive_user(db: AsyncSession, user: User, actor_id: uuid.UUID | None = None) -> None:
    """Ausgetretenen Mitarbeiter ins Archiv verschieben.

    Die Personalakte samt Historie (Stempelungen, Antraege, Lohndaten) bleibt
    vollstaendig in der DB — Aufbewahrungspflicht. Der Mitarbeiter wird aber
    ueberall aus dem aktiven System genommen:
    - employment_status='archived', is_active=False -> faellt aus allen Listen,
      Dropdowns, Reports und dem Export (die filtern auf is_active).
    - NFC-Karte wird geloest (Karte kann neu vergeben werden, stempeln mit der
      alten Karte ist ausgeschlossen).
    - Verknuepfte Login-Accounts werden deaktiviert.
    Alles mit Audit-Eintrag (inkl. Austrittsdatum/-grund und geloester Karte).
    """
    from zeiterfassung.models.account import Account

    freed_badge = user.nfc_badge_id
    user.employment_status = "archived"
    user.is_active = False
    user.nfc_badge_id = None
    user.updated_at = datetime.now(tz=UTC)

    accounts = (
        (
            await db.execute(
                tenant_scoped(select(Account), Account, user.tenant_id).where(
                    Account.employee_id == user.id,
                    Account.is_active == True,  # noqa: E712
                )
            )
        )
        .scalars()
        .all()
    )
    for acc in accounts:
        acc.is_active = False
        # Zusaetzlich zum is_active-Check: laufende Tokens/Sessions sofort kappen.
        await invalidate_account_tokens(db, acc)

    if freed_badge:
        await _sync_badge_pool(db, user.tenant_id, freed=freed_badge)

    await db.flush()
    await audit_service.log(
        db,
        tenant_id=user.tenant_id,
        actor_id=actor_id,
        action="user.archive",
        resource_type="user",
        resource_id=user.id,
        details={
            "termination_date": user.termination_date.isoformat()
            if user.termination_date
            else None,
            "termination_reason": user.termination_reason,
            "nfc_badge_freigegeben": freed_badge,
            "accounts_deaktiviert": len(accounts),
        },
    )


async def unarchive_user(db: AsyncSession, user: User, actor_id: uuid.UUID | None = None) -> None:
    """Wiedereintritt: Mitarbeiter aus dem Archiv zurueckholen.

    Login-Accounts werden bewusst NICHT automatisch reaktiviert (das
    entscheidet ein Admin unter Einstellungen -> Benutzerkonten), die
    NFC-Karte muss neu zugewiesen werden.
    """
    user.employment_status = "active"
    user.is_active = True
    user.termination_reason = None
    user.updated_at = datetime.now(tz=UTC)
    await db.flush()
    await audit_service.log(
        db,
        tenant_id=user.tenant_id,
        actor_id=actor_id,
        action="user.unarchive",
        resource_type="user",
        resource_id=user.id,
        details={},
    )


async def archive_due_terminations(db: AsyncSession) -> int:
    """Alle faelligen Austritte archivieren (Austrittsdatum < heute).

    Idempotent — laeuft taeglich im Hintergrund und faengt den Fall ab, dass
    ein in der Zukunft gesetztes Austrittsdatum erreicht wird, ohne dass
    jemand die Personalakte anfasst. Gibt die Anzahl archivierter MA zurueck.
    """
    from datetime import date as _date

    due = (
        (
            await db.execute(
                select(User).where(
                    User.is_active == True,  # noqa: E712
                    User.termination_date.is_not(None),
                    User.termination_date < _date.today(),
                )
            )
        )
        .scalars()
        .all()
    )
    for user in due:
        await archive_user(db, user, actor_id=None)
    if due:
        await db.commit()
    return len(due)


_SCOPE_PRIORITY: dict[str, int] = {
    "global": 4,
    "department": 3,
    "self": 2,
    "anonymized_global": 1,
}


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


async def _issue_token_pair(
    db: AsyncSession,
    account: Account,
    tenant_id: uuid.UUID,
    *,
    family_id: uuid.UUID | None = None,
    user_agent: str | None = None,
) -> TokenPair:
    """Rollen/Rechte laden und ein frisches Token-Paar fuer ``account`` ausstellen.

    Gemeinsam genutzt von ``authenticate()`` und ``refresh_tokens()``, damit ein
    Refresh niemals andere (insbesondere: weniger) Claims erhaelt als ein Login —
    sonst wird ein User nach jedem Refresh faktisch rechtlos.

    ``tenant_id`` ist der bei Login/Refresh angefragte Tenant; er wird nur fuer
    Accounts ohne festen ``tenant_id`` (Cross-Tenant-Superadmin) als effektiver
    Tenant verwendet — sonst zaehlt immer ``account.tenant_id``.

    Legt zusaetzlich eine ``refresh_sessions``-Zeile fuer den neuen Refresh-Token
    an (Rotation/Reuse-Detection, Bead fkw.2): ``family_id=None`` (Login) startet
    eine neue Familie, ein uebergebenes ``family_id`` (Refresh-Rotation) haengt
    die neue Zeile an die bestehende Familie.
    """
    from zeiterfassung.models.account import AccountRole
    from zeiterfassung.models.user import Role as _Role

    roles = list(
        (
            await db.execute(
                select(_Role)
                .join(AccountRole, _Role.id == AccountRole.role_id)
                .where(AccountRole.account_id == account.id)
            )
        )
        .scalars()
        .all()
    )

    perms: set[str] = set()
    for role in roles:
        if role.permissions:
            perms.update(role.permissions)
    permissions = list(perms)

    scopes = [role.data_scope for role in roles if role.data_scope]
    data_scope = max(scopes, key=lambda s: _SCOPE_PRIORITY.get(s, 0)) if scopes else "self"
    role_type: str | None = next((role.role_type for role in roles if role.role_type), None)

    # Effective tenant: the account's tenant, or the requested one for a superadmin.
    eff_tenant = account.tenant_id or tenant_id

    # Zustaendige Bereiche: explizit am Konto gepflegt (scope_department_ids)
    # oder — Standardfall — die Abteilung der verknuepften Personalakte.
    department_ids: list[str] = [str(x) for x in (account.scope_department_ids or []) if x]
    employee_id = account.employee_id
    if employee_id:
        emp = (await db.execute(select(User).where(User.id == employee_id))).scalar_one_or_none()
        if not department_ids and emp is not None and emp.department_id:
            department_ids = [str(emp.department_id)]
    department_id: str | None = department_ids[0] if department_ids else None

    access_token = create_access_token(
        user_id=str(account.id),  # subject = account id
        tenant_id=str(eff_tenant),
        permissions=permissions,
        department_id=department_id,
        department_ids=department_ids or None,
        role_type=role_type,
        data_scope=data_scope,
        employee_id=str(employee_id) if employee_id else None,
        is_superadmin=bool(account.is_superadmin),
        token_version=account.token_version or 0,
    )
    jti = uuid.uuid4().hex
    refresh_token = create_refresh_token(
        user_id=str(account.id),
        tenant_id=str(eff_tenant),
        jti=jti,
    )

    from zeiterfassung.config import get_settings
    from zeiterfassung.models.refresh_session import RefreshSession

    s = get_settings()
    now = datetime.now(tz=UTC)
    session = RefreshSession(
        jti=jti,
        account_id=account.id,
        tenant_id=eff_tenant,
        family_id=family_id or uuid.uuid4(),
        created_at=now,
        expires_at=now + timedelta(days=s.jwt_refresh_token_expire_days),
        user_agent=user_agent,
    )
    db.add(session)
    await db.flush()

    return TokenPair(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=s.jwt_access_token_expire_minutes * 60,
    )


async def authenticate(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    email: str,
    password: str,
    *,
    user_agent: str | None = None,
) -> TokenPair:
    """Verify credentials and return JWT tokens.

    The access token includes department_id, role_type, and data_scope claims
    in addition to the existing permissions list.  data_scope is resolved as
    the highest-priority scope across all assigned roles so that a user with
    both 'self' and 'department' scoped roles gets 'department'.

    Legt eine neue ``refresh_sessions``-Familie an (``user_agent`` wird vom
    Endpoint aus dem Request-Header durchgereicht, siehe fkw.2).

    Login-Lockout (fkw.4, DB-basiert): ``LOGIN_LOCKOUT_THRESHOLD`` Fehlversuche
    in Folge sperren den Account fuer ``LOGIN_LOCKOUT_DURATION``. Ein gesperrter
    Account bekommt dieselbe generische Fehlermeldung wie falsche Zugangsdaten
    (kein Lockout-Orakel). Die Zustandsaenderung (Zaehler/Sperre) wird auch bei
    einem fehlgeschlagenen Versuch committet — sonst geht sie mit der
    geworfenen Exception verloren und das Lockout greift nie.
    """
    # Login is against ACCOUNTS (separate from employees in `users`).
    from zeiterfassung.models.account import Account

    now = datetime.now(tz=UTC)

    account = (
        await db.execute(select(Account).where(Account.email == email.lower()))
    ).scalar_one_or_none()
    if account is None or not account.is_active:
        raise InvalidCredentialsError("Invalid email or password")

    # SQLite (Tests) gibt DateTime(timezone=True) naiv zurueck; Postgres liefert
    # tz-aware Werte (gleiches Muster wie bei RefreshSession.expires_at).
    locked_until = account.locked_until
    if locked_until is not None:
        if locked_until.tzinfo is None:
            locked_until = locked_until.replace(tzinfo=UTC)
        if locked_until > now:
            raise InvalidCredentialsError("Invalid email or password")

    if not account.hashed_password or not verify_password(password, account.hashed_password):
        account.failed_login_count = (account.failed_login_count or 0) + 1
        just_locked = account.failed_login_count >= LOGIN_LOCKOUT_THRESHOLD
        if just_locked:
            account.locked_until = now + LOGIN_LOCKOUT_DURATION
            account.failed_login_count = 0
            await audit_service.log(
                db,
                tenant_id=account.tenant_id or tenant_id,
                actor_id=account.employee_id,
                action="account.locked",
                resource_type="account",
                resource_id=account.id,
                details={"email": account.email},
            )
        await db.commit()
        raise InvalidCredentialsError("Invalid email or password")

    # Erfolgreicher Login: Lockout-Zustand zuruecksetzen.
    account.failed_login_count = 0
    account.locked_until = None

    token_response = await _issue_token_pair(db, account, tenant_id, user_agent=user_agent)

    eff_tenant = account.tenant_id or tenant_id
    await audit_service.log(
        db,
        tenant_id=eff_tenant,
        actor_id=account.employee_id,  # the person behind the login (None for admin accounts)
        action="account.login",
        resource_type="account",
        resource_id=account.id,
        details={"email": account.email},
    )
    await db.commit()

    return token_response


async def _revoke_family(db: AsyncSession, family_id: uuid.UUID) -> None:
    """Alle noch aktiven (nicht revozierten) Sessions einer Familie revoken.

    In-place-``UPDATE`` auf ``revoked_at`` ist hier bewusst zulaessig — die
    Session-Tabelle ist operativer Zustand, kein append-only Lohn-/Zeit-Datum
    (siehe Docstring von ``RefreshSession``).
    """
    from zeiterfassung.models.refresh_session import RefreshSession

    now = datetime.now(tz=UTC)
    rows = (
        (
            await db.execute(
                select(RefreshSession).where(
                    RefreshSession.family_id == family_id,
                    RefreshSession.revoked_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        row.revoked_at = now


async def refresh_tokens(
    db: AsyncSession, refresh_token: str, *, user_agent: str | None = None
) -> TokenPair:
    """Neues Token-Paar aus einem gueltigen Refresh-Token ausstellen (Rotation).

    Prueft zusaetzlich zur JWT-Signatur, dass der Account in der DB noch
    existiert und ``is_active`` ist — sonst wuerde ein deaktivierter Account
    bis zu ``jwt_refresh_token_expire_days`` Tage nach der Sperrung weiter
    gueltige Access-Token bekommen. Claims werden ueber denselben Pfad wie
    beim Login neu aufgebaut (``_issue_token_pair``), damit ein Refresh nie
    Rechte "vergisst".

    Rotation + Reuse-Detection (fkw.2): der Refresh-Token traegt eine ``jti``,
    die auf eine ``refresh_sessions``-Zeile zeigt.
    - Zeile lebt (nicht revoked/expired) -> rotieren: alte Zeile revoken
      (``revoked_at`` + ``rotated_to_jti``), neue Zeile mit derselben
      ``family_id`` anlegen.
    - Zeile bereits revoked/rotiert -> Replay eines gestohlenen Tokens: die
      GESAMTE Familie wird revoked, ein Audit-Log-Eintrag geschrieben, 401.
    - Keine Zeile (oder Token ohne ``jti`` — Alt-Token vor fkw.2) -> 401; diese
      Tokens werden mit dem Deploy bewusst invalidiert (kleine Pilotgruppe).
    """
    import jwt as _jwt

    from zeiterfassung.models.account import Account
    from zeiterfassung.models.refresh_session import RefreshSession

    try:
        payload = decode_refresh_token(refresh_token)
    except _jwt.InvalidTokenError as exc:
        raise InvalidCredentialsError(str(exc)) from exc

    try:
        account_id = uuid.UUID(str(payload["sub"]))
    except (KeyError, ValueError) as exc:
        raise InvalidCredentialsError("Invalid refresh token") from exc

    token_tenant_id = payload.get("tenant_id")
    jti = payload.get("jti")

    account = (
        await db.execute(select(Account).where(Account.id == account_id))
    ).scalar_one_or_none()
    if account is None or not account.is_active:
        raise InvalidCredentialsError("Account not found or inactive")

    # tenant_id-Claim muss zum Account passen: fest zugeordnete Accounts
    # (nicht Cross-Tenant-Superadmin) duerfen per Refresh nicht auf einen
    # anderen Tenant "springen".
    if account.tenant_id is not None and str(account.tenant_id) != str(token_tenant_id):
        raise InvalidCredentialsError("Tenant mismatch")

    try:
        eff_tenant_id = uuid.UUID(str(token_tenant_id)) if token_tenant_id else account.tenant_id
    except ValueError as exc:
        raise InvalidCredentialsError("Invalid refresh token") from exc
    if eff_tenant_id is None:
        raise InvalidCredentialsError("Invalid refresh token")

    if not jti:
        # Alt-Token vor fkw.2: keine JTI, keine Session-Zeile -> zurueckweisen.
        raise InvalidCredentialsError("Invalid refresh token")

    session = (
        await db.execute(select(RefreshSession).where(RefreshSession.jti == jti))
    ).scalar_one_or_none()
    if session is None:
        raise InvalidCredentialsError("Invalid refresh token")

    now = datetime.now(tz=UTC)

    if session.revoked_at is not None:
        # Reuse-Detection: dieser Token wurde bereits rotiert/revoked und taucht
        # trotzdem wieder auf -> vermutlich gestohlen. Ganze Familie sperren.
        await _revoke_family(db, session.family_id)
        await audit_service.log(
            db,
            tenant_id=eff_tenant_id,
            actor_id=account.employee_id,
            action="auth.refresh_reuse_detected",
            resource_type="account",
            resource_id=account.id,
            details={"family_id": str(session.family_id), "jti": jti},
        )
        await db.commit()
        raise InvalidCredentialsError("Refresh token reuse detected")

    # SQLite (Tests) gibt DateTime(timezone=True) naiv zurueck; Postgres liefert
    # tz-aware Werte. Vor dem Vergleich vereinheitlichen.
    expires_at = session.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at <= now:
        raise InvalidCredentialsError("Refresh token expired")

    token_response = await _issue_token_pair(
        db, account, eff_tenant_id, family_id=session.family_id, user_agent=user_agent
    )
    new_payload = decode_refresh_token(token_response.refresh_token)
    session.revoked_at = now
    session.rotated_to_jti = new_payload["jti"]
    await db.commit()

    return token_response


async def invalidate_account_tokens(db: AsyncSession, account: Account) -> None:
    """Alle laufenden Tokens eines Accounts sofort entwerten (login-hardening).

    token_version-Bump macht jedes ausgestellte Access-Token beim naechsten
    Request ungueltig (get_current_user vergleicht den ver-Claim); zusaetzlich
    werden ALLE aktiven Refresh-Sessions des Accounts revoked (alle Familien,
    nicht nur eine) — sonst wuerde ein gestohlener Refresh-Cookie z. B. einen
    Passwortwechsel ueberleben. Kein commit — der Aufrufer committet.
    """
    from zeiterfassung.models.refresh_session import RefreshSession

    account.token_version = (account.token_version or 0) + 1
    now = datetime.now(tz=UTC)
    rows = (
        (
            await db.execute(
                select(RefreshSession).where(
                    RefreshSession.account_id == account.id,
                    RefreshSession.revoked_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        row.revoked_at = now


async def revoke_refresh_session(db: AsyncSession, refresh_token: str | None) -> None:
    """Logout: die gesamte Session-Familie des Refresh-Tokens revoken.

    Best-effort — fehlender/ungueltiger/abgelaufener Token wird still ignoriert
    (Logout liefert immer 204, egal ob der Token noch gueltig war).
    """
    import jwt as _jwt

    from zeiterfassung.models.refresh_session import RefreshSession

    if not refresh_token:
        return

    try:
        payload = decode_refresh_token(refresh_token)
    except _jwt.InvalidTokenError:
        return

    jti = payload.get("jti")
    if not jti:
        return

    session = (
        await db.execute(select(RefreshSession).where(RefreshSession.jti == jti))
    ).scalar_one_or_none()
    if session is None:
        return

    await _revoke_family(db, session.family_id)

    # Logout entwertet auch die laufenden Access-Tokens des Accounts sofort
    # (login-hardening). Nur der Version-Bump — andere Geraete behalten ihre
    # Refresh-Familie und holen sich beim naechsten 401 still einen frischen
    # Token (bestehender 401->Refresh->Retry-Pfad im Frontend).
    from zeiterfassung.models.account import Account

    account = (
        await db.execute(select(Account).where(Account.id == session.account_id))
    ).scalar_one_or_none()
    if account is not None:
        account.token_version = (account.token_version or 0) + 1


# ---------------------------------------------------------------------------
# Router-facing helpers (moved verbatim out of api/v1/users.py, zeiterfassung-6pcv)
# ---------------------------------------------------------------------------


async def resolve_single_tenant(db: AsyncSession) -> uuid.UUID:
    """Auto-resolve the tenant when exactly one exists (single-tenant mode).

    Used by ``POST /auth/login`` — moved verbatim from the former inline query.
    """
    from fastapi import HTTPException, status
    from sqlalchemy import func

    from zeiterfassung.models.tenant import Tenant

    count_result = await db.execute(select(func.count()).select_from(Tenant))
    tenant_count = count_result.scalar_one()

    if tenant_count == 1:
        tenant_result = await db.execute(select(Tenant.id).limit(1))
        return tenant_result.scalar_one()

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Multiple tenants exist. Use POST /auth/login/{tenant_id}.",
    )


async def change_own_password(
    db: AsyncSession, account_id: uuid.UUID, current_password: str, new_password: str
) -> None:
    """Eigenes Account-Passwort ändern (aktuelles Passwort wird geprüft).

    Moved verbatim from ``POST /auth/change-password``.
    """
    from fastapi import HTTPException, status

    from zeiterfassung.models.account import Account

    if len(new_password) < 8:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Neues Passwort muss mindestens 8 Zeichen haben.",
        )
    acc = (await db.execute(select(Account).where(Account.id == account_id))).scalar_one_or_none()
    if acc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")
    if not acc.hashed_password or not verify_password(current_password, acc.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Aktuelles Passwort ist falsch.",
        )
    acc.hashed_password = hash_password(new_password)
    await invalidate_account_tokens(db, acc)
    await db.commit()


async def export_own_data(db: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID) -> dict:
    """DSGVO Art. 15 — export all personal data for the authenticated user.

    Moved verbatim from ``GET /users/me/export``.
    """
    from zeiterfassung.models.absence import ApprovalAuditLog, ApprovalRequest
    from zeiterfassung.models.clock import DailyTimesheet, PunchEvent
    from zeiterfassung.schemas.user import UserResponse

    uid = user_id
    tid = tenant_id

    # Profile
    user = await get_user_by_id(db, tid, uid)
    profile = UserResponse.model_validate(user).model_dump(mode="json")

    # Punch events
    pe_result = await db.execute(
        tenant_scoped(select(PunchEvent), PunchEvent, tid)
        .where(
            PunchEvent.employee_id == uid,
        )
        .order_by(PunchEvent.event_time, PunchEvent.id)
    )
    punch_events = [
        {
            "id": e.id,
            "event_type": e.event_type.value,
            "event_time": e.event_time.isoformat(),
            "source": e.source.value,
            "device_id": e.device_id,
            "created_at": e.created_at.isoformat(),
        }
        for e in pe_result.scalars().all()
    ]

    # Daily timesheets
    ts_result = await db.execute(
        tenant_scoped(select(DailyTimesheet), DailyTimesheet, tid)
        .where(
            DailyTimesheet.employee_id == uid,
        )
        .order_by(DailyTimesheet.work_date)
    )
    timesheets = [
        {
            "work_date": str(t.work_date),
            "net_work_minutes": t.net_work_minutes,
            "break_minutes": t.break_minutes,
            "overtime_minutes": t.overtime_minutes,
            "status": t.status.value,
        }
        for t in ts_result.scalars().all()
    ]

    # Absence requests
    ar_result = await db.execute(
        tenant_scoped(select(ApprovalRequest), ApprovalRequest, tid)
        .where(
            ApprovalRequest.employee_id == uid,
        )
        .order_by(ApprovalRequest.date_from)
    )
    absence_requests = [
        {
            "id": str(r.id),
            "request_type": r.request_type.value,
            "status": r.status.value,
            "date_from": str(r.date_from),
            "date_to": str(r.date_to),
            "days_count": float(r.days_count),
            "submitted_at": r.submitted_at.isoformat() if r.submitted_at else None,
            "decided_at": r.decided_at.isoformat() if r.decided_at else None,
            "decision_reason": r.decision_reason,
        }
        for r in ar_result.scalars().all()
    ]

    # Approval audit log entries where this user was the actor
    al_result = await db.execute(
        select(ApprovalAuditLog)
        .join(ApprovalRequest, ApprovalAuditLog.request_id == ApprovalRequest.id)
        .where(
            ApprovalRequest.tenant_id == tid,
            ApprovalAuditLog.actor_id == uid,
        )
        .order_by(ApprovalAuditLog.created_at)
    )
    audit_entries = [
        {
            "request_id": str(al.request_id),
            "old_status": al.old_status.value if al.old_status else None,
            "new_status": al.new_status.value,
            "created_at": al.created_at.isoformat(),
        }
        for al in al_result.scalars().all()
    ]

    return {
        "export_generated_at": __import__("datetime")
        .datetime.now(__import__("datetime").timezone.utc)
        .isoformat(),
        "profile": profile,
        "punch_events": punch_events,
        "timesheets": timesheets,
        "absence_requests": absence_requests,
        "approval_audit_entries": audit_entries,
    }


async def get_auth_me_data(db: AsyncSession, current_user: CurrentUser) -> dict:
    """Return user profile + permissions + scope claims from JWT.

    Moved verbatim from ``GET /auth/me``. Includes department_id, department_name,
    role_type, and data_scope so the frontend can adapt navigation without an
    extra request.
    """
    from zeiterfassung.models.account import Account
    from zeiterfassung.models.department import Department
    from zeiterfassung.models.user import User as _User

    account = (
        await db.execute(select(Account).where(Account.id == current_user.account_id))
    ).scalar_one_or_none()
    if account is None:
        from fastapi import HTTPException, status

        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")

    # Optional linked employee (self-service); admins have none.
    emp = None
    if account.employee_id:
        emp = (
            await db.execute(select(_User).where(_User.id == account.employee_id))
        ).scalar_one_or_none()

    if emp is not None:
        first_name, last_name, full_name = emp.first_name, emp.last_name, emp.full_name
    elif account.is_superadmin or current_user.role_type == "admin":
        first_name, last_name, full_name = "System", "Admin", "System Admin"
    else:
        local = account.email.split("@")[0]
        first_name, last_name, full_name = local, "", local

    dept_id = current_user.department_id or (emp.department_id if emp else None)
    department_name: str | None = None
    if dept_id is not None:
        dept = (
            await db.execute(select(Department).where(Department.id == dept_id))
        ).scalar_one_or_none()
        if dept is not None:
            department_name = dept.name

    return {
        "id": account.id,
        "tenant_id": current_user.tenant_id,
        "email": account.email,
        "first_name": first_name,
        "last_name": last_name,
        "full_name": full_name,
        "employee_number": emp.employee_number if emp else None,
        "nfc_badge_id": emp.nfc_badge_id if emp else None,
        "is_active": account.is_active,
        "contracted_hours_per_week": emp.contracted_hours_per_week if emp else 0.0,
        "department_id": dept_id,
        "manager_id": emp.manager_id if emp else None,
        "time_capture": emp.time_capture if emp else "nfc",
        "auto_hours_per_day": emp.auto_hours_per_day if emp else 8.0,
        "employment_status": emp.employment_status if emp else "active",
        "work_schedule_id": emp.work_schedule_id if emp else None,
        "hire_date": emp.hire_date if emp else None,
        "employment_type": emp.employment_type if emp else "hourly",
        "vacation_entitlement_days": emp.vacation_entitlement_days if emp else 0.0,
        "created_at": account.created_at,
        "updated_at": account.updated_at,
        "permissions": current_user.permissions,
        "department_name": department_name,
        "role_type": current_user.role_type,
        "data_scope": current_user.data_scope,
        "employee_id": current_user.employee_id,
        "is_superadmin": current_user.is_superadmin,
    }


async def get_user_with_department_scope_check(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    is_own: bool,
    has_read_all: bool,
    has_read_team: bool,
    caller_department_ids: list[uuid.UUID] | None,
) -> User:
    """Get a user by ID, enforcing the department-scope check for Teamleiter.

    Moved verbatim from ``GET /users/{user_id}``. Permission-namespace checks
    (``is_own``/``has_read_all``/``has_read_team``) are resolved by the caller
    (router) since they need the ``AuthUser`` object; this only does the
    remaining query-backed scope check + lookup.
    """
    from fastapi import HTTPException, status

    if not is_own and not has_read_all and not has_read_team:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions"
        )

    if not is_own and has_read_team and not has_read_all:
        # Verify the target is in the same department
        if not caller_department_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No department assigned to your account",
            )
        result = await db.execute(
            tenant_scoped(select(User), User, tenant_id).where(
                User.id == user_id,
                User.department_id.in_(caller_department_ids),
            )
        )
        if result.scalar_one_or_none() is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Target user is not in your department",
            )

    return await get_user_by_id(db, tenant_id, user_id)


class UserHasHistoryError(Exception):
    """Endgueltiges Loeschen blockiert: Mitarbeiter hat Zeit-/Lohndaten."""


class UserNotDeletedError(Exception):
    """Endgueltiges Loeschen blockiert: Mitarbeiter ist nicht soft-geloescht."""


async def delete_user_hard(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    actor_id: uuid.UUID | None = None,
) -> None:
    """Einen soft-geloeschten Mitarbeiter endgueltig aus der DB entfernen.

    Bewusst eng gefasst (Aufbewahrungspflicht): nur erlaubt, wenn der
    Mitarbeiter bereits ``employment_status == "deleted"`` ist UND keinerlei
    Zeit-/Lohndaten existieren (Stempelungen, Tagesboegen, Antraege,
    Ueberstunden-Eintraege, Lohnbuchungen). Damit ist Hard-Delete faktisch
    nur fuer falsch angelegte Datensaetze; echte Austritte laufen ueber das
    Archiv (Austrittsdatum). Konfig-Reste haengen per FK CASCADE/SET NULL
    (ein verknuepftes Login-Konto verliert nur die Verknuepfung).
    """
    from zeiterfassung.models.absence import ApprovalRequest
    from zeiterfassung.models.clock import DailyTimesheet, PunchEvent
    from zeiterfassung.models.overtime import OvertimeEntry
    from zeiterfassung.models.payroll_booking import PayrollBooking

    user = await get_scoped(db, User, tenant_id, user_id)
    if user is None:
        raise UserNotFoundError(user_id)
    if user.employment_status != "deleted":
        raise UserNotDeletedError(
            "Mitarbeiter ist nicht geloescht. Zuerst auf der Detailseite "
            "loeschen (ausblenden); regulaere Austritte ueber das "
            "Austrittsdatum archivieren."
        )

    history_models = (PunchEvent, DailyTimesheet, ApprovalRequest, OvertimeEntry, PayrollBooking)
    for model in history_models:
        result = await db.execute(select(model.id).where(model.employee_id == user_id).limit(1))
        if result.scalar_one_or_none() is not None:
            raise UserHasHistoryError(
                "Mitarbeiter hat Zeit-/Lohndaten - bitte archivieren statt "
                "endgueltig loeschen (Aufbewahrungspflicht)."
            )

    # Verknuepfte Login-Konten explizit entkoppeln (nicht auf DB-seitiges
    # ON DELETE SET NULL verlassen — SQLite in Tests erzwingt FKs nicht).
    from zeiterfassung.models.account import Account as _Account

    linked = await db.execute(
        tenant_scoped(select(_Account), _Account, tenant_id).where(_Account.employee_id == user_id)
    )
    for acc in linked.scalars().all():
        acc.employee_id = None

    details = {"name": user.full_name, "employee_number": user.employee_number}
    await db.delete(user)
    await audit_service.log(
        db,
        tenant_id=tenant_id,
        actor_id=actor_id,
        action="user.delete",
        resource_type="user",
        resource_id=user_id,
        details=details,
    )
