"""User, Role, and UserRole models."""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import Boolean, Date, ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from zeiterfassung.models.base import (
    Base,
    JSONBCompat,
    TimestampMixin,
    UUIDCompat,
    UUIDPrimaryKeyMixin,
)


class Role(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Permission role within a tenant."""

    __tablename__ = "roles"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUIDCompat(),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    permissions: Mapped[list[str] | None] = mapped_column(
        JSONBCompat, nullable=True, comment="Array of permission strings"
    )
    data_scope: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="self",
        server_default="self",
        comment="global | department | self | anonymized_global",
    )
    role_type: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
        comment="gf | hr | buchhaltung | teamleiter | mitarbeiter | betriebsrat",
    )

    users: Mapped[list[UserRole]] = relationship("UserRole", back_populates="role", lazy="select")

    def __repr__(self) -> str:
        return f"<Role id={self.id} name={self.name!r}>"


class UserRole(Base):
    """Many-to-many join table between users and roles."""

    __tablename__ = "user_roles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUIDCompat(),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        UUIDCompat(),
        ForeignKey("roles.id", ondelete="CASCADE"),
        primary_key=True,
    )

    user: Mapped[User] = relationship("User", back_populates="user_roles", lazy="select")
    role: Mapped[Role] = relationship("Role", back_populates="users", lazy="select")


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Employee user account within a tenant."""

    __tablename__ = "users"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUIDCompat(),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Optional — Mitarbeiter ohne Firmen-E-Mail sind der Normalfall im Lager.
    email: Mapped[str | None] = mapped_column(String(320), nullable=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String(512), nullable=False)
    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_name: Mapped[str] = mapped_column(String(100), nullable=False)
    employee_number: Mapped[str | None] = mapped_column(String(50), nullable=True)
    nfc_badge_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, index=True, comment="NFC card UID"
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Monatlicher Stundenzettel-Versand per E-Mail (Spec 2026-08-07):
    # Haekchen in Einstellungen -> Stundenzettel-Versand.
    monthly_timesheet_email: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    contracted_hours_per_week: Mapped[float] = mapped_column(
        Numeric(5, 2), nullable=False, default=40.0
    )
    # Optionales wochentagsspezifisches Tages-Soll (Mo..So = Index 0..6) in MINUTEN.
    # Ist es gesetzt, gilt es je Wochentag; sonst Fallback contracted_hours_per_week/5
    # (Mo–Fr) bzw. 0 (Sa/So). Beispiel: [495,495,495,495,330,0,0] = Mo–Do 8:15, Fr 5:30.
    weekday_target_minutes: Mapped[list | None] = mapped_column(JSONBCompat, nullable=True)
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDCompat(),
        ForeignKey("departments.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDCompat(),
        ForeignKey("companies.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    manager_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDCompat(),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        comment="Direct supervisor (who approves this employee)",
    )
    # Arbeitszeit-Profil (work_time_profiles): serverseitig typgebunden an
    # time_capture — kind 'nfc' nur bei nfc/web, kind 'auto' nur bei auto.
    work_time_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDCompat(),
        ForeignKey("work_time_profiles.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    time_capture: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        default="nfc",
        server_default="nfc",
        comment="Zeiterfassung: auto | web | nfc",
    )
    auto_hours_per_day: Mapped[float] = mapped_column(
        Numeric(4, 2),
        nullable=False,
        default=8.0,
        server_default="8.0",
        comment="Used when time_capture == 'auto'",
    )

    employment_status: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        default="active",
        server_default="active",
        comment="active | paused | deleted (soft-delete) | archived (Austritt)",
    )

    # Abrechnungsprofil (tenant.settings['wage_profiles']): abweichende
    # Lohnart-Zuordnungen fuer diesen Mitarbeiter; None = Standard-Zuordnung.
    wage_profile_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    work_schedule_id: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
        comment=(
            "Zugewiesenes Arbeitszeitmodell (id aus tenant.settings.work_schedules); "
            "None = Standard"
        ),
    )

    hire_date: Mapped[date | None] = mapped_column(
        Date,
        nullable=True,
        comment="Eintrittsdatum in die Firma (Dienstalter, Firmenjubilaeum)",
    )
    counting_start_date: Mapped[date | None] = mapped_column(
        Date,
        nullable=True,
        comment="Erfassungsbeginn: ab hier zaehlen Ueberstunden/Fehlzeiten/Konto",
    )
    termination_date: Mapped[date | None] = mapped_column(
        Date,
        nullable=True,
        comment="Austrittsdatum: ab hier zaehlen keine Ueberstunden/Fehlzeiten mehr",
    )
    termination_reason: Mapped[str | None] = mapped_column(
        String(40),
        nullable=True,
        comment="Austrittsgrund (Aufhebungsvertrag, Kuendigung, Rente, ...)",
    )
    birth_date: Mapped[date | None] = mapped_column(Date, nullable=True, comment="Geburtstag")
    employment_type: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="hourly",
        server_default="hourly",
        comment=(
            "Beschaeftigungsart: salaried (Gehalt) | hourly (Stundenlohn) | "
            "werkstudent (nur Zeiterfassung: kein Tagessoll; Export = Ist-Stunden auf Zeitlohn)"
        ),
    )
    vacation_entitlement_days: Mapped[float] = mapped_column(
        Numeric(4, 1),
        nullable=False,
        default=30.0,
        server_default="30",
        comment="Urlaubsanspruch in Tagen pro Jahr",
    )
    # Resturlaub fürs EINTRITTSJAHR (bei Anstellung erfasst). Im Jahr des
    # hire_date bildet dieser Wert die Bilanz-Basis (statt des vollen
    # Jahresanspruchs) — z. B. anteilig bei Mitte-des-Jahres-Eintritt. Ab dem
    # Folgejahr gilt wieder vacation_entitlement_days. None = kein Sonderwert.
    initial_vacation_remaining_days: Mapped[float | None] = mapped_column(
        Numeric(4, 1),
        nullable=True,
        comment="Resturlaub im Eintrittsjahr (Bilanz-Basis für hire_date.year)",
    )
    overtime_payout_mode: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default="above_cap",
        server_default="above_cap",
        comment=(
            "Überstunden-Auszahlung: none | all | above_cap | forfeit_above_cap "
            "(Überlauf über der Grenze verfällt, statt ausgezahlt zu werden)"
        ),
    )
    overtime_cap_hours: Mapped[float] = mapped_column(
        Numeric(5, 1),
        nullable=False,
        default=20.0,
        server_default="20",
        comment="Kappungsgrenze (h) für Modus above_cap",
    )

    user_roles: Mapped[list[UserRole]] = relationship(
        "UserRole", back_populates="user", lazy="select"
    )

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"

    def __repr__(self) -> str:
        return f"<User id={self.id} email={self.email!r}>"
