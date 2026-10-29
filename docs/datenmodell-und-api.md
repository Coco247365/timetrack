# Datenmodell und API (Referenz, deutsch)

Ausführliche Referenz zu Tabellen, Enums und Endpunkten. Überblick und Schnellstart: siehe [README](../README.md).

## Datenmodell

PostgreSQL, async. Alle Fachtabellen tragen `tenant_id` (Multi-Tenancy). Zeitstempel durchgängig
`TIMESTAMPTZ` (UTC); Tagesgrenzen werden im App-Layer in Tenant-Zeitzone aufgelöst.
Event-/Log-Tabellen nutzen `BIGINT`-PKs (Insert/Range-Performance), Entitäten `UUID`.
Migrationen additiv über Alembic (ein fortlaufender Head).

**Portables Typ-Mapping** (`models/base.py`): `UUIDCompat` (PG `UUID`, sonst `String(36)`),
`JSONBCompat` (PG `JSONB`, sonst `JSON`), `BigIntCompat` (PG `BIGINT`, sonst `INTEGER`) — erlaubt
Postgres in Produktion und SQLite in Tests. `UUIDPrimaryKeyMixin` (`id` = uuid4-PK),
`TimestampMixin` (`created_at`/`updated_at`, `server_default now()`).

### Enums

| Enum | Werte |
|---|---|
| `EventType` | PUNCH_IN, PUNCH_OUT, BREAK_START, BREAK_END |
| `EventSource` | NFC_TERMINAL, PWA_MOBILE, WEB_PORTAL, ADMIN_MANUAL, SYSTEM_AUTO |
| `TimesheetStatus` | DRAFT, COMPLETE, FLAGGED, APPROVED, LOCKED |
| `RequestType` | VACATION, SICK_LEAVE, UNPAID_LEAVE, SPECIAL_LEAVE, CORRECTION |
| `RequestStatus` | DRAFT, SUBMITTED, APPROVED, REJECTED, ESCALATED, CANCELLED |
| `DayPart` | FULL, AM, PM (Halbtags-Urlaub; `native_enum=False`) |
| `CorrectionType` | MANUAL_TIME, BREAK_ADJUSTMENT, STATUS_CHANGE, NOTE_UPDATE, SUPPLEMENT_CORRECTION |
| `ExportFormat` / `ExportStatus` | datev_csv / PREVIEW, GENERATED, DELIVERED, FAILED |
| `ActorType` | USER, SYSTEM |

### Tabellen

**Mandant & Identität**

- **tenants** — Firma/Mandant. `name String(255)`, `timezone String(64)="Europe/Berlin"`,
  `locale String(10)="de-DE"`, `bundesland String(4)="HH"`, `settings JSONB` (Arbeitszeitmodelle,
  Zuschlagssätze, Feiertags-Land …), `is_active bool`.
- **accounts** — Login-Identität, getrennt von der Belegschaft. `email String(320)`,
  `hashed_password String(255)` (Argon2id), `is_superadmin bool`, `is_active bool`,
  `tenant_id → tenants(CASCADE)` (nullable = Cross-Tenant-Superadmin),
  `employee_id → users(SET NULL)` (optionaler Personen-Link; Admin = Account ohne Employee).
- **account_roles** — M:N accounts↔roles (Composite-PK, beide FKs `CASCADE`).
- **roles** — `name String(100)`, `permissions JSONB` (Array von Permission-Strings),
  `data_scope String(20)="self"` (global | department | self | anonymized_global),
  `role_type String(20)` (gf | hr | buchhaltung | teamleiter | mitarbeiter | betriebsrat).
- **users** — Belegschaft/„employees" (Stamm- und Zeitdaten hängen hier).
  `email`, `hashed_password String(512)`, `first_name`/`last_name`, `employee_number`,
  `nfc_badge_id String(128)` (idx), `contracted_hours_per_week Numeric(5,2)=40`,
  `vacation_entitlement_days Numeric(4,1)=30`, `time_capture String(10)="nfc"` (auto|web|nfc),
  `auto_hours_per_day Numeric(4,2)=8`, `employment_status="active"` (active|paused|deleted),
  `employment_type="hourly"` (salaried|hourly), `overtime_payout_mode="above_cap"`
  (none|all|above_cap), `overtime_cap_hours Numeric(5,1)=20`, `hire_date`, `birth_date`,
  `work_schedule_id`, FKs `department_id`/`company_id`/`manager_id` (je `SET NULL`).
- **user_roles** — M:N users↔roles (Legacy-Rollenzuordnung der Belegschaft).
- **departments** — `name`, `code String(10)`, `teamleiter_user_id → users(SET NULL)`,
  Unique `(tenant_id, code)`.
- **companies** — Firmen-Gruppierung innerhalb eines Mandanten (DATEV): `advisor_number`
  (Berater-Nr.), `client_number` (Mandanten-Nr.).

**Zeiterfassung**

- **punch_events** — append-only Stempel-Events (`BIGINT`-PK, nie UPDATE/DELETE).
  `event_type EventType`, `event_time TIMESTAMPTZ` (idx), `source EventSource`,
  `device_id`, `client_event_id String(64)` (Idempotenz für Offline-Queue), `ip_address`.
  Korrekturen: `corrects_event_id → punch_events(SET NULL)` ersetzt ein früheres Event,
  `is_void bool` löscht es logisch; `created_by`, `reason`.
- **daily_timesheets** — 1 Zeile/MA/Tag, aus Events berechnet (`BIGINT`-PK).
  `work_date` (idx), `start_time`/`end_time`, `break_minutes`, `net_work_minutes`,
  `gross_work_minutes`, `overtime_minutes`, `status TimesheetStatus=DRAFT`,
  `is_night_work`/`is_sunday_work`/`is_holiday_work`, `supplement_data JSONB` (Zuschläge),
  `flagged_reason`, `computed_from_events bool`, `approved_by`/`approved_at`/`locked_at`.
  Unique `(tenant_id, employee_id, work_date)`. **Source of truth für Payroll/DATEV.**
- **timesheet_corrections** — Audit jeder Timesheet-Korrektur (`BIGINT`-PK).
  `timesheet_id → daily_timesheets(CASCADE)`, `correction_type CorrectionType`,
  `field_changed`/`old_value`/`new_value`/`reason`, `requested_by`, `approved_by`.
- **monthly_summaries** — Monatsaggregat für schnellen DATEV-Export (`BIGINT`-PK).
  `year_month Date` (1. des Monats), `total_work_minutes`, `total_overtime_minutes`,
  `days_worked`/`days_sick`/`days_vacation`/`days_holiday`,
  `night_/sunday_/holiday_supplement_minutes`, `arbeitszeitkonto_balance_minutes`,
  `datev_exported_at`. Unique `(tenant_id, employee_id, year_month)`.

**Abwesenheit & Freigaben**

- **approval_requests** — Abwesenheits-/Korrekturanträge (UUID-PK).
  `request_type RequestType`, `status RequestStatus=DRAFT`, `date_from`/`date_to`,
  `day_part DayPart=FULL`, `days_count Numeric(6,2)`, `payload JSONB` (z. B. AU-Upload-Ref),
  `notes`, `submitted_at`, `decided_by`/`decided_at`/`decision_reason`.
- **approval_audit_log** — immutabler Status-Übergangs-Trail (`BIGINT`-PK):
  `request_id → approval_requests(CASCADE)`, `old_status`/`new_status`, `actor_id`,
  `actor_type ActorType`, `notes`.
- **approval_delegations** — Vertretung: `delegator_id`/`delegate_id → users(CASCADE)`,
  `valid_from`/`valid_to`. CheckConstraints: `valid_to ≥ valid_from`, `delegator ≠ delegate`.

**Lohn & Export**

- **payroll_exports** — DATEV-Export-Läufe (UUID-PK). `year_month`, `format ExportFormat`,
  `status ExportStatus=PREVIEW`, `file_checksum String(64)` (SHA-256),
  `file_content LargeBinary` (Original-LODAS-Datei), `row_count`, `generated_by`,
  `generated_at`/`delivered_at`, `error_message`.
- **wage_type_mappings** — interner Code → DATEV-Lohnart (Integer-PK).
  `internal_code String(64)` (z. B. REGULAR_HOURS), `datev_lohnart Integer` (NULL = kein Export),
  `unit String(10)="hours"` (hours|amount|days), `is_active`.
- **overtime_ledger** — datiertes, append-only Überstunden-Log (UUID-PK).
  `year_month`, `entry_date` (Replay-Reihenfolge), `entry_type String(12)`
  (accrual|payout|adjustment), `hours Numeric(7,2)` (signiert), `reason`, `created_by`.
- **payroll_bookings** — manuelle Lohnbuchungen (UUID-PK). `year_month`,
  `wage_type_code`, `value Numeric(10,2)` (darf negativ), `booking_date`, `note`, `created_by`.

**Compliance**

- **audit_log** — systemweiter Security-/Activity-Trail (`BIGINT`-PK, append-only).
  `actor_id`, `action String(128)` (idx), `resource_type`/`resource_id`, `ip_address`,
  `details JSONB`, `data_category String(20)` (personal|medical|financial|aggregate|system),
  `access_reason String(500)` (Pflicht bei `data_category=medical`).

**FK-Integrität:** Fachdaten hängen mit `ondelete=CASCADE` am Mandanten/Mitarbeiter.
Audit-/Entscheider-Referenzen (`decided_by`, `approved_by`, `actor_id`, `generated_by`,
`requested_by`) haben bewusst **kein** `ondelete` (Historie bleibt erhalten);
`overtime_ledger.created_by` und `payroll_bookings.created_by` sind reine UUID-Spalten ohne FK.

---

## API

Alle Routen unter **`/api/v1`**. Fehlerformat `{"detail": …}`; Statuscodes
400/401/403/404/409 (Konflikt/ungültiger Statusübergang)/422.

### Authentifizierung (`api/deps.py`)

- **JWT-Bearer** (`AuthUser`): `Authorization: Bearer <token>`. Access-Token 15 min, Claims
  `sub`=account_id, `tenant_id`, `permissions[]`, `department_id`, `role_type`, `data_scope`,
  `employee_id`, `is_superadmin`. `has_permission(exact)` und `has_permission_namespace("admin"→admin:*)`.
- **Device-Key** (`DeviceID`): NFC-Terminal, Header `X-Device-Api-Key` (+ `X-Tenant-Id` bei
  NFC-Endpunkten). Argon2id/HS256 gelten hier nicht; DB-Validierung des Keys ist offen (TODO).
- **ScopeContext** (`get_scope`): aus `data_scope` abgeleitete Sichtbarkeit —
  global > department > self > anonymized_global (Betriebsrat).
- **MedicalUser** (`require_medical_access`): `medical:read` **und** Header `X-Access-Reason`
  (≥3 Zeichen, wird auditiert) für Krankheits-Details.

### `POST /auth/*` — öffentlich

`POST /auth/login` (Single-Tenant Auto-Resolve) · `POST /auth/login/{tenant_id}` ·
`POST /auth/refresh` · `POST /auth/logout` · `POST /auth/change-password`.
Login → `TokenResponse` (access + refresh).

### `/clock` — Stempeln & Timesheets

| Methode + Pfad | Auth / Permission | Zweck |
|---|---|---|
| POST `/clock/punch` | `timesheet:view_own` | Eigener Stempel (Zeit serverseitig) |
| POST `/clock/nfc` · `/nfc/auto` · `/nfc/preview` · `/nfc/undo` | Device-Key + Tenant | Terminal-Stempel per Badge (idempotent via `client_event_id`), Smart-Detect, Vorschau, Undo (Grace-Window) |
| POST `/clock/nfc/profile` · `/nfc/leave-request` | Device-Key + Tenant | Terminal: Live-Profil / Urlaubsantrag |
| GET `/clock/events` | `timesheet:view_own` | Roh-Events des Callers |
| GET `/clock/timesheet` · `/timesheet/{employee_id}` | eigen `view_own`; fremd `view_all`/`view_team`/`admin` | Tagesbögen (scope-gefiltert) |
| GET `/clock/status` | eigen `view_own`; fremd `view_all`/`view_team` | Ein-/Aus-Status + Live-Minuten |
| GET `/clock/presence` | `timesheet:view_all`/`view_team` | Live-Anwesenheit |
| POST `/clock/recompute/{work_date}` | `timesheet:view_all`/`admin` | Timesheet aus Events neu berechnen |
| GET `/clock/day-events/{employee_id}` | `timesheet:edit_all`/`edit_team`/`admin` | Tages-Log + Timesheet |
| POST `/clock/manual-event` · `/manual-break` | `timesheet:edit_*` | Event/Pause nachtragen (append) |
| POST `/clock/events/{id}/correct` · `/void` | `timesheet:edit_*` | Event ersetzen / logisch löschen (append) |

### `/absences` — Anträge

| Methode + Pfad | Auth / Permission | Zweck |
|---|---|---|
| POST `/absences/requests` | `leave:request_own` (auto-approve bei `leave:approve_all`) | Antrag stellen |
| POST `/absences/admin/requests` | `leave:approve_all`/`users:write`/`admin` | Abwesenheit FÜR MA erfassen (rückdatierbar) |
| GET `/absences/requests` · `/requests/{id}` | scope-gefiltert; medical-strip ohne `medical:read` | Anträge listen / einzeln |
| PATCH `/absences/requests/{id}/approve` · `/reject` | `leave:approve_all`/`approve_team` (gleiche Abt.); keine Selbstgenehmigung | Entscheiden |
| PATCH `/absences/requests/{id}/cancel` | Owner (`leave:request_own`) | Eigenen Antrag stornieren |
| POST `/absences/requests/{id}/revoke` · `/request-cancellation` · `/decline-cancellation` | Genehmiger bzw. Owner | Widerruf / Storno-Workflow |
| GET `/absences/balance` | eigen frei; fremd scope-abhängig | Urlaubssaldo (Tage **und** Stunden) |
| GET `/absences/calendar` · `/team-capacity` | scope-gefiltert | Kalender / Team-Kapazität |

### `/users`, Accounts, Stammdaten, Überstunden

| Methode + Pfad | Auth / Permission | Zweck |
|---|---|---|
| GET `/users/me` · `/auth/me` · `/users/me/export` | AuthUser | Eigenes Profil / Claims / DSGVO-Export (Art. 15) |
| GET/POST `/users` · GET/PATCH `/users/{id}` | `users:read_*`/`users:write` (scope) | Belegschaft verwalten |
| GET/POST/PATCH `/accounts` · POST `/accounts/{id}/reset-password` | `admin:settings`/`users:write` (Superadmin nur durch Superadmin) | Logins verwalten |
| GET/POST/PATCH/DELETE `/departments` · `/companies` · `/roles` | `admin:settings`/`users:write` | Bereiche, Firmen, Rollen |
| GET `/admin/employees` · `/admin/scanned-cards` | `users:read_*`/`reports:view` | Voll-MA-Sicht / NFC-Karten-Pool |
| GET `/overtime/balance` · `/balances` · `/ledger` · POST `/overtime/adjustment` | eigen frei; fremd `_admin_can_read`; Korrektur `overtime:manage` | Überstunden-Konto |
| GET/POST/DELETE `/payroll/bookings` | `payroll:read`/`payroll:write` | Manuelle Lohnbuchungen |

### `/admin` — Auswertung & Einstellungen

| Methode + Pfad | Auth / Permission | Zweck |
|---|---|---|
| GET `/admin/dashboard` | `admin:dashboard` | Kennzahlen |
| GET `/admin/flagged` | `timesheet:view_all` | Geflaggte Tage (ArbZG/„Gehen fehlt") |
| GET `/admin/pending-approvals` · POST `/admin/bulk-approve` | `leave:approve_all`/`approve_team` | Genehmigungs-Queue / Massenfreigabe |
| GET `/admin/approval-context/{id}` | `leave:approve_*` | Kontext für Genehmigung (Saldo, Overlaps, Besetzung) |
| GET `/admin/monthly-report` | `timesheet:view_all`/`view_team`/`reports:view` | Monatssummen je MA |
| GET/POST/DELETE `/admin/wage-types` · `/surcharge-config` · `/wage-type-assignments` · `/overtime-config` · `/work-schedules` | `admin:settings` | Lohnarten, Zuschläge, Überstundenregeln, Arbeitszeitmodelle |
| GET `/admin/audit-log` | `audit:read`/`admin` | Vollständiges Audit-Log |

### `/export` — DATEV/LODAS

`POST /export/datev/preview` · `POST /export/datev/generate` · `GET /export/datev/history` ·
`GET /export/datev/{id}/download` (CSV, Windows-1252) — je `export:datev`/`admin`.
Lohnarten u. a. Grundlohn 1000, Überstunden 1610/1620, Urlaub 2000, Krankheit 2100.

### `/br` — Betriebsrat

Nur mit `timesheet:view_aggregates` **und** `role_type == betriebsrat` (schließt Buchhaltung aus):
`GET /br/overtime-stats` · `/br/absence-stats` (anonyme Aggregate) ·
`/br/audit-trail` (anonymisiert, Rolle statt Name; `audit:read_anonymized`).

### `/holidays`

`GET /holidays?year=` — Feiertage im Bundesland des Tenants (ISO-Datum → Name).

---

