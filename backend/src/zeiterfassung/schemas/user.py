"""User and auth schemas."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field, field_validator


def _validate_weekday_target(v: list | None) -> list[int] | None:
    """Wochentags-Soll: genau 7 Werte (Mo..So) in Minuten, je 0..1440 — oder None."""
    if v is None:
        return None
    if len(v) != 7:
        raise ValueError("weekday_target_minutes braucht genau 7 Werte (Mo..So)")
    out: list[int] = []
    for m in v:
        iv = int(m)
        if not 0 <= iv <= 1440:
            raise ValueError("Tages-Soll muss zwischen 0 und 1440 Minuten liegen")
        out.append(iv)
    return out


# Austrittsgründe (kanonische Schlüssel) — das Frontend zeigt die Labels an.
TerminationReason = Literal[
    "AUFHEBUNGSVERTRAG",
    "AUSBILDUNG_ENDE",
    "BEFRISTUNG_ENDE",
    "KUENDIGUNG",
    "KUENDIGUNG_MITARBEITER",
    "KUENDIGUNG_UNTERNEHMEN",
    "RENTE",
    "TOD",
]


# Erlaubte Auszahl-Modi (Spiegel des Dropdowns, siehe
# .claude/rules/ui-limits-serverseitig-spiegeln.md).
_PAYOUT_MODES = "^(none|all|above_cap|forfeit_above_cap)$"


class UserCreate(BaseModel):
    # Optional: nicht jeder Mitarbeiter hat eine (Firmen-)Adresse. Leer bleibt
    # leer — es wird kein Platzhalter erzeugt.
    email: EmailStr | None = None
    # Optional: Mitarbeiter (Personalakte) brauchen kein Passwort — Logins
    # laufen ueber die getrennte accounts-Tabelle. Nur noch fuer Alt-Clients.
    password: str | None = Field(default=None, min_length=8, max_length=128)
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    employee_number: str | None = Field(default=None, max_length=50)
    nfc_badge_id: str | None = Field(default=None, max_length=128)
    contracted_hours_per_week: float = Field(default=40.0, ge=0, le=168)
    # Optionales Tages-Soll je Wochentag (Mo..So, Minuten); None = Wochenstunden÷5.
    weekday_target_minutes: list[int] | None = None
    department_id: uuid.UUID | None = None
    company_id: uuid.UUID | None = None
    role_id: uuid.UUID | None = None
    manager_id: uuid.UUID | None = None
    time_capture: str = Field(default="nfc", pattern="^(auto|web|nfc)$")
    auto_hours_per_day: float = Field(default=8.0, ge=0, le=24)
    work_schedule_id: str | None = Field(default=None, max_length=36)
    wage_profile_id: str | None = Field(default=None, max_length=36)
    hire_date: date | None = None
    counting_start_date: date | None = None
    termination_date: date | None = None
    termination_reason: TerminationReason | None = None
    birth_date: date | None = None
    employment_type: str = Field(default="hourly", pattern="^(salaried|hourly|werkstudent)$")
    # None = kein expliziter Wert -> Mandanten-Default (admin_settings_service.
    # get_vacation_settings / absence_service.resolve_default_vacation_entitlement_days).
    vacation_entitlement_days: float | None = Field(default=None, ge=0, le=365)
    # Resturlaub beim Systemstart (optional); Bilanz-Basis im Jahr des
    # Erfassungsbeginns, sonst im Eintrittsjahr (siehe get_vacation_balance).
    initial_vacation_remaining_days: float | None = Field(default=None, ge=0, le=365)
    # Werte-Liste serverseitig spiegeln (.claude/rules/ui-limits-serverseitig-
    # spiegeln.md): das Dropdown ist Komfort, die API ist direkt aufrufbar.
    overtime_payout_mode: str = Field(default="above_cap", pattern=_PAYOUT_MODES)
    overtime_cap_hours: float = Field(default=20.0, ge=0, le=999)

    _v_weekday = field_validator("weekday_target_minutes")(_validate_weekday_target)


class UserUpdate(BaseModel):
    email: EmailStr | None = None
    first_name: str | None = Field(default=None, min_length=1, max_length=100)
    last_name: str | None = Field(default=None, min_length=1, max_length=100)
    employee_number: str | None = None
    nfc_badge_id: str | None = None
    contracted_hours_per_week: float | None = Field(default=None, ge=0, le=168)
    weekday_target_minutes: list[int] | None = None
    department_id: uuid.UUID | None = None
    company_id: uuid.UUID | None = None
    role_id: uuid.UUID | None = None
    manager_id: uuid.UUID | None = None
    time_capture: str | None = Field(default=None, pattern="^(auto|web|nfc)$")
    auto_hours_per_day: float | None = Field(default=None, ge=0, le=24)
    # Arbeitszeit-Profil: serverseitig typgebunden an time_capture (422 bei
    # Mismatch, siehe user_service._check_work_time_profile_binding).
    work_time_profile_id: uuid.UUID | None = None
    employment_status: str | None = Field(default=None, pattern="^(active|paused|deleted)$")
    is_active: bool | None = None
    work_schedule_id: str | None = Field(default=None, max_length=36)
    wage_profile_id: str | None = Field(default=None, max_length=36)
    hire_date: date | None = None
    counting_start_date: date | None = None
    termination_date: date | None = None
    termination_reason: TerminationReason | None = None
    birth_date: date | None = None
    employment_type: str | None = Field(default=None, pattern="^(salaried|hourly|werkstudent)$")
    vacation_entitlement_days: float | None = Field(default=None, ge=0, le=365)
    initial_vacation_remaining_days: float | None = Field(default=None, ge=0, le=365)
    overtime_payout_mode: str | None = Field(default=None, pattern=_PAYOUT_MODES)
    overtime_cap_hours: float | None = Field(default=None, ge=0, le=999)

    _v_weekday = field_validator("weekday_target_minutes")(_validate_weekday_target)


class UserResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    tenant_id: uuid.UUID
    # Optional — Mitarbeiter ohne Firmen-E-Mail (Migration 0041).
    email: str | None = None
    first_name: str
    last_name: str
    full_name: str
    employee_number: str | None
    nfc_badge_id: str | None
    is_active: bool
    contracted_hours_per_week: float
    weekday_target_minutes: list[int] | None = None
    department_id: uuid.UUID | None = None
    company_id: uuid.UUID | None = None
    manager_id: uuid.UUID | None = None
    time_capture: str = "nfc"
    auto_hours_per_day: float = 8.0
    work_time_profile_id: uuid.UUID | None = None
    employment_status: str = "active"
    work_schedule_id: str | None = None
    wage_profile_id: str | None = None
    hire_date: date | None = None
    counting_start_date: date | None = None
    termination_date: date | None = None
    termination_reason: str | None = None
    birth_date: date | None = None
    employment_type: str = "hourly"
    vacation_entitlement_days: float = 30.0
    initial_vacation_remaining_days: float | None = None
    overtime_payout_mode: str = "above_cap"
    overtime_cap_hours: float = 20.0
    created_at: datetime
    updated_at: datetime


class AuthMeResponse(UserResponse):
    """Extended response for /auth/me — includes permissions and scope from JWT."""

    permissions: list[str] = []
    department_id: uuid.UUID | None = None
    company_id: uuid.UUID | None = None
    department_name: str | None = None
    role_type: str | None = None
    data_scope: str = "self"
    employee_id: uuid.UUID | None = None  # verknuepfter Mitarbeiter (Self-Service)
    is_superadmin: bool = False


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    """Public shape of login/refresh responses.

    The refresh token is NOT part of this — it only ever travels as the
    httpOnly ``zeit_refresh`` cookie (fkw.4, cookie-only transition end; see
    docs/plan-jwt-login-haertung.md Phase 6). Internally the service layer
    still needs the raw refresh token value to set that cookie — see
    ``zeiterfassung.services.user_service.TokenPair``.
    """

    access_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds until access token expiry
