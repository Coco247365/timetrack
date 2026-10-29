//! Blocking HTTP client for the Zeiterfassung backend NFC endpoints.
//!
//! The kiosk uses a two-phase flow:
//! 1. [`BackendClient::preview`] — non-recording lookup of the employee and
//!    their current clock state (drives the choice screen).
//! 2. [`BackendClient::commit`] — records the explicitly chosen event.

use std::time::Duration;

use serde::{Deserialize, Serialize};
use thiserror::Error;

use crate::config::Config;

/// The punch action the backend recorded (or would record, for a preview).
///
/// On the wire the preview's `next_action` is lowercase (`clock_in`,
/// `clock_out`, `break_end`); the commit endpoint echoes the uppercase
/// `event_type` (`PUNCH_IN`, `PUNCH_OUT`, `BREAK_START`, `BREAK_END`). We accept
/// both schemes. Unknown strings are preserved in [`Action::Other`] so they can
/// still be surfaced.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Action {
    PunchIn,
    PunchOut,
    BreakStart,
    BreakEnd,
    Other(String),
}

impl Action {
    /// Parse a backend action string (case-insensitive, both naming schemes).
    fn parse(raw: &str) -> Self {
        match raw.trim().to_ascii_lowercase().as_str() {
            "clock_in" | "punch_in" => Self::PunchIn,
            "clock_out" | "punch_out" => Self::PunchOut,
            "break_start" => Self::BreakStart,
            "break_end" => Self::BreakEnd,
            _ => Self::Other(raw.to_owned()),
        }
    }

    /// The `event_type` string the commit endpoint expects for this action.
    ///
    /// Returns `None` for [`Action::Other`], which cannot be committed.
    #[must_use]
    pub fn event_type(&self) -> Option<&'static str> {
        match self {
            Self::PunchIn => Some("PUNCH_IN"),
            Self::PunchOut => Some("PUNCH_OUT"),
            Self::BreakStart => Some("BREAK_START"),
            Self::BreakEnd => Some("BREAK_END"),
            Self::Other(_) => None,
        }
    }

    /// Heading text for the success screen, using the employee's first name.
    #[must_use]
    pub fn heading_de(&self, first: &str) -> String {
        match self {
            Self::PunchIn => format!("Willkommen, {first}!"),
            Self::PunchOut => format!("Schönen Feierabend, {first}!"),
            Self::BreakStart => format!("Gute Pause, {first}!"),
            Self::BreakEnd => format!("Willkommen zurück, {first}!"),
            Self::Other(_) => first.to_owned(),
        }
    }

    /// Human-facing German label for the kiosk display (the `message` line).
    #[must_use]
    pub fn label_de(&self) -> &str {
        match self {
            Self::PunchIn => "Kommen erfasst",
            Self::PunchOut => "Gehen erfasst",
            Self::BreakStart => "Pause begonnen",
            Self::BreakEnd => "Pause beendet",
            Self::Other(s) => s,
        }
    }

}

/// The employee's current clock state, as returned by the preview endpoint.
///
/// This drives the kiosk's branching: `ClockedOut`/`OnBreak` commit immediately,
/// `ClockedIn` shows the Pause/Feierabend choice screen.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CurrentState {
    ClockedOut,
    ClockedIn,
    OnBreak,
    Other(String),
}

impl CurrentState {
    fn parse(raw: &str) -> Self {
        match raw.trim().to_ascii_lowercase().as_str() {
            "clocked_out" => Self::ClockedOut,
            "clocked_in" => Self::ClockedIn,
            "on_break" => Self::OnBreak,
            _ => Self::Other(raw.to_owned()),
        }
    }

    /// Short German label for the profile screen's status line.
    #[must_use]
    pub fn label_de(&self) -> &str {
        match self {
            Self::ClockedOut => "ausgestempelt",
            Self::ClockedIn => "arbeitet",
            Self::OnBreak => "in Pause",
            Self::Other(s) => s,
        }
    }
}

/// Successful result of a committed punch.
#[derive(Debug, Clone)]
pub struct PunchResult {
    pub employee_name: String,
    /// Returned by the backend; not shown in the current UI flow.
    #[allow(dead_code)]
    pub employee_number: String,
    pub action: Action,
    /// ISO-8601 timestamp string as returned by the backend.
    pub event_time: String,
    /// The badge that was punched.
    #[allow(dead_code)]
    pub badge_id: String,
    /// Persoenliche Kiosk-Hinweise, die nach dem Stempeln gezeigt werden (#189).
    pub personal_messages: Vec<String>,
}

/// Result of a non-executing preview.
#[derive(Debug, Clone)]
pub struct PreviewResult {
    pub employee_name: String,
    #[allow(dead_code)]
    pub employee_number: String,
    #[allow(dead_code)]
    pub badge_id: String,
    pub current_state: CurrentState,
    /// The backend's suggested next action. Retained from the preview response
    /// for completeness; the menu flow derives buttons from `current_state`.
    #[allow(dead_code)]
    pub next_action: Action,
    /// Terminal personalize-mode gate: true when this token is flagged as an
    /// admin fob in `tenant.settings["admin_cards"]` (backend-side, revocable
    /// — never an on-chip attribute). A dedicated admin fob with no assigned
    /// employee still resolves here (rather than 404ing) with placeholder
    /// `employee_name`/`current_state`/`next_action` — see `main.rs`'s
    /// `dispatch_preview`, which routes an admin tap into
    /// `personalize_mode::transition` instead of the normal menu flow.
    pub is_admin: bool,
}

/// Live profile summary for the "Profil anschauen" screen.
///
/// Mirrors the employee's web profile: current clock state, today's worked /
/// break / overtime minutes, the running Arbeitszeitkonto balance and the
/// remaining vacation for the year. All numeric fields are raw (minutes / days);
/// the UI layer formats them for display.
#[derive(Debug, Clone)]
pub struct ProfileResult {
    pub employee_name: String,
    pub current_state: CurrentState,
    /// Erste Kommt-Zeit heute (ISO), falls schon gestempelt wurde.
    pub today_start: Option<String>,
    /// Letzte Geht-Zeit heute (ISO); None solange nicht ausgestempelt.
    pub today_end: Option<String>,
    pub today_work_minutes: i64,
    pub today_break_minutes: i64,
    pub today_overtime_minutes: i64,
    /// Running working-time-account balance (may be negative).
    pub konto_balance_minutes: i64,
    /// Contracted weekly hours as the backend reports it (a decimal string such
    /// as `"40.00"`), or `None` when unset.
    pub contracted_hours_per_week: Option<String>,
    /// Remaining vacation days for the current year, when available.
    pub vacation_remaining_days: Option<f64>,
}

/// Result of a vacation request submitted from the terminal.
#[derive(Debug, Clone)]
pub struct LeaveResult {
    /// Backend status, e.g. `"SUBMITTED"`.
    #[allow(dead_code)]
    pub status: String,
    /// Working days the request spans, as computed by the backend.
    pub days_count: f64,
    /// Remaining vacation days for the year after this request, if available.
    pub vacation_remaining_days: Option<f64>,
}

/// Extract the first whitespace-delimited token of a full name.
///
/// Used to greet employees by first name only; this also drops a trailing
/// `(Demo)` suffix on demo users. Falls back to the whole (trimmed) string when
/// there is no whitespace.
#[must_use]
pub fn first_name(full_name: &str) -> &str {
    full_name
        .split_whitespace()
        .next()
        .unwrap_or(full_name.trim())
}

/// Errors surfaced to the kiosk UI.
#[derive(Debug, Error)]
pub enum ApiError {
    /// Unknown / inactive badge (HTTP 404).
    #[error("{0}")]
    UnknownBadge(String),
    /// Invalid state transition (HTTP 409).
    #[error("{0}")]
    InvalidTransition(String),
    /// Rate limited — scanned too quickly (HTTP 429).
    #[error("{0}")]
    RateLimited(String),
    /// Any other non-success HTTP status, carrying the backend `detail`.
    #[error("{detail}")]
    Http { status: u16, detail: String },
    /// Network / transport failure (could not reach the backend).
    #[error("Verbindungsfehler: {0}")]
    Transport(String),
}

impl ApiError {
    /// Whether this error means "the backend is temporarily unavailable/
    /// unhealthy, try this exact punch again later" as opposed to "the
    /// backend looked at this specific punch and permanently rejected it".
    ///
    /// Used by the offline-queue flush ([`crate::queue::OfflineQueue`]) to
    /// decide whether to keep an entry queued (transient) or dead-letter it
    /// (permanent) — see `flush_queue` in `main.rs`. Getting this wrong in
    /// the transient direction only delays a retry; getting it wrong in the
    /// permanent direction silently drops a real punch, so unrecognised
    /// non-2xx statuses (`Http { status, .. }` outside 4xx) default to
    /// transient rather than being dropped.
    #[must_use]
    pub fn is_transient(&self) -> bool {
        match self {
            ApiError::Transport(_) => true,
            // The terminal itself hit the backend's rate limiter — not a
            // rejection of this punch's content, just bad timing. Retrying
            // later resolves it; dropping it would lose a real punch.
            ApiError::RateLimited(_) => true,
            // 5xx (and any other status this client doesn't recognise as a
            // deliberate 4xx rejection) means the backend itself is
            // unhealthy, not that it rejected the punch on its merits.
            ApiError::Http { status, .. } => *status >= 500,
            // The backend evaluated this exact punch against a specific
            // employee/state and rejected it — retrying won't change that.
            ApiError::UnknownBadge(_) | ApiError::InvalidTransition(_) => false,
        }
    }
}

/// Body for `/nfc/preview`.
#[derive(Debug, Serialize)]
struct PreviewRequest<'a> {
    badge_id: &'a str,
}

/// Body for `/nfc` (the recording endpoint).
///
/// `event_time`/`client_event_id` are only set when replaying from the offline
/// queue: the backend then books the punch at the original tap time and
/// deduplicates retries.
#[derive(Debug, Serialize)]
struct CommitRequest<'a> {
    badge_id: &'a str,
    event_type: &'a str,
    #[serde(skip_serializing_if = "Option::is_none")]
    event_time: Option<&'a str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    client_event_id: Option<&'a str>,
}

/// Body for `/nfc/auto` (state-deriving endpoint, used for queued taps whose
/// action was unknown because the backend was already unreachable at tap time).
#[derive(Debug, Serialize)]
struct AutoRequest<'a> {
    badge_id: &'a str,
    event_time: &'a str,
    client_event_id: &'a str,
}

/// FastAPI error envelope: `{ "detail": "..." }`.
#[derive(Debug, Deserialize)]
struct DetailBody {
    detail: Option<DetailValue>,
}

/// `detail` may be a string or a structured value depending on the error path.
#[derive(Debug, Deserialize)]
#[serde(untagged)]
enum DetailValue {
    Text(String),
    Other(serde_json::Value),
}

impl DetailValue {
    /// Menschenlesbarer Text fuer den Terminal-Bildschirm.
    ///
    /// Strukturierte Backend-Fehler (`{"code": ..., "message": ...}`) wurden
    /// frueher als komplettes JSON angezeigt — auf dem Terminal stand dann
    /// z. B. `{"code":"invalid_punch_state","message":"Cannot record …"}`
    /// (#185). Jetzt: bekannte Codes bekommen einen deutschen Text,
    /// sonst wird `message` genommen; JSON nur als letzter Ausweg.
    fn into_text(self) -> String {
        match self {
            Self::Text(s) => s,
            Self::Other(v) => {
                if let Some(code) = v.get("code").and_then(|c| c.as_str()) {
                    if let Some(text) = german_for_code(code) {
                        return text.to_owned();
                    }
                }
                v.get("message")
                    .and_then(|m| m.as_str())
                    .map(str::to_owned)
                    .unwrap_or_else(|| v.to_string())
            }
        }
    }
}

/// Deutsche Klartext-Meldung fuer bekannte Backend-Fehlercodes.
fn german_for_code(code: &str) -> Option<&'static str> {
    match code {
        "invalid_punch_state" => Some("Buchung nicht möglich – bitte zuerst einstempeln."),
        "punch_rate_limited" => Some("Zu schnell hintereinander – bitte kurz warten."),
        "no_badge_user" => Some("Diese Karte ist keinem Mitarbeiter zugeordnet."),
        "nothing_to_undo" => Some("Nichts zum Rückgängigmachen."),
        _ => None,
    }
}

#[derive(Debug, Deserialize)]
struct CommitResponse {
    employee_name: String,
    employee_number: String,
    event_type: String,
    event_time: String,
    /// Persoenliche Kiosk-Hinweise fuer diesen MA (#189). Fehlt bei
    /// aelteren Backends -> leer.
    #[serde(default)]
    personal_messages: Vec<String>,
}

/// Antwort von GET /clock/kiosk-messages (Rundmeldungen fuer den Ruhebildschirm).
#[derive(Debug, Deserialize)]
struct KioskMessagesResponse {
    #[serde(default)]
    messages: Vec<String>,
}

#[derive(Debug, Deserialize)]
struct PreviewResponse {
    employee_name: String,
    employee_number: String,
    badge_id: String,
    current_state: String,
    next_action: String,
    /// Absent on older backends — defaults to `false` so a not-yet-upgraded
    /// backend just never offers personalize mode (safe default).
    #[serde(default)]
    is_admin: bool,
}

/// Nested vacation block of the `/nfc/profile` response.
#[derive(Debug, Deserialize)]
struct VacationBody {
    #[allow(dead_code)]
    entitlement_days: f64,
    #[allow(dead_code)]
    used_days: f64,
    #[allow(dead_code)]
    pending_days: f64,
    remaining_days: f64,
}

/// Body for `/nfc/leave-request`.
#[derive(Debug, Serialize)]
struct LeaveRequestBody<'a> {
    badge_id: &'a str,
    date_from: &'a str,
    date_to: &'a str,
}

#[derive(Debug, Deserialize)]
struct LeaveResponse {
    status: String,
    days_count: f64,
    vacation_remaining_days: Option<f64>,
}

/// Body for `POST /nfc/register-to-pool` (used by the `sam_personalize` CLI,
/// bead 4ty, and by the interactive kiosk's enroll/404-backfill paths, bead
/// rst).
///
/// `uids` is parallel-indexed to `token_ids` (same length) — an entry may be
/// `null` when the chip UID is unavailable for that token. The backend
/// upserts: a token already pooled without a `chip_uid` gets it backfilled
/// on a later call, without creating a duplicate pool entry.
#[derive(Debug, Serialize)]
struct RegisterToPoolBody<'a> {
    token_ids: &'a [String],
    uids: &'a [Option<String>],
}

/// Result of a `register_to_pool` batch call.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RegisterToPoolResult {
    pub pooled: u32,
    pub skipped_existing: u32,
    pub skipped_assigned: u32,
    /// Number of already-pooled tokens whose `chip_uid` was backfilled by
    /// this call (bead rst).
    pub backfilled: u32,
    pub pool_size: u32,
}

#[derive(Debug, Deserialize)]
struct RegisterToPoolResponse {
    pooled: u32,
    skipped_existing: u32,
    skipped_assigned: u32,
    /// Absent on an older backend — defaults to `0` (same defaulting
    /// convention as `PreviewResponse::is_admin`).
    #[serde(default)]
    backfilled: u32,
    pool_size: u32,
}

/// Body for `POST /nfc/register-admin-cards` (used by the `sam_personalize
/// --admin` CLI and the terminal's own personalize mode).
#[derive(Debug, Serialize)]
struct RegisterAdminCardsBody<'a> {
    token_ids: &'a [String],
}

/// Result of a `register_admin_cards` batch call.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RegisterAdminCardsResult {
    pub added: u32,
    pub already_admin: u32,
    pub admin_count: u32,
}

#[derive(Debug, Deserialize)]
struct RegisterAdminCardsResponse {
    added: u32,
    already_admin: u32,
    admin_count: u32,
}

#[derive(Debug, Deserialize)]
struct ProfileResponse {
    employee_name: String,
    current_state: String,
    #[serde(default)]
    today_start: Option<String>,
    #[serde(default)]
    today_end: Option<String>,
    today_work_minutes: i64,
    today_break_minutes: i64,
    today_overtime_minutes: i64,
    arbeitszeitkonto_balance_minutes: i64,
    // The backend serialises the Numeric column as a JSON string ("40.00").
    contracted_hours_per_week: Option<String>,
    vacation: Option<VacationBody>,
}

/// Blocking backend client. Construct once and share behind an `Arc`.
pub struct BackendClient {
    http: reqwest::blocking::Client,
    base_url: String,
    tenant_id: String,
    device_key: String,
}

impl BackendClient {
    /// Build a client from resolved [`Config`].
    ///
    /// # Errors
    ///
    /// Returns [`ApiError::Transport`] if the underlying HTTP client cannot be
    /// constructed (e.g. TLS backend initialisation failure).
    pub fn new(config: &Config) -> Result<Self, ApiError> {
        // KIOSK_INSECURE_TLS=1: selbstsigniertes Server-Zertifikat akzeptieren.
        // Noetig fuer den Zugriff per nackter VPN-IP (Caddy-interne CA auf
        // dem Server). Nur im VPN vertretbar — mit echter Domain wieder entfernen.
        let insecure = std::env::var("KIOSK_INSECURE_TLS")
            .map(|v| v == "1" || v.eq_ignore_ascii_case("true"))
            .unwrap_or(false);
        // 4 s statt 10 s: im LAN/VPN antwortet der Server in Millisekunden —
        // der Timeout greift praktisch nur, wenn er weg ist, und bestimmt
        // damit, wie lange "Einen Moment…" im Offline-Fall steht (max. 2x4 s).
        let http = reqwest::blocking::Client::builder()
            .timeout(Duration::from_secs(4))
            .danger_accept_invalid_certs(insecure)
            .build()
            .map_err(|e| ApiError::Transport(e.to_string()))?;

        Ok(Self {
            http,
            base_url: config.backend_url.clone(),
            tenant_id: config.tenant_id.clone(),
            device_key: config.device_key.clone(),
        })
    }

    /// Phase 1: preview the employee and their current clock state without
    /// recording anything.
    ///
    /// # Errors
    ///
    /// Returns an [`ApiError`] mapped from the HTTP status (404/409/429/other)
    /// carrying the backend `detail` text, or [`ApiError::Transport`] on a
    /// network failure.
    pub fn preview(&self, badge_id: &str) -> Result<PreviewResult, ApiError> {
        let url = format!("{}/api/v1/clock/nfc/preview", self.base_url);
        let body = PreviewRequest { badge_id };
        let resp = self.send(&url, &body)?;
        let resp = self.check_status(resp)?;
        let body: PreviewResponse = resp
            .json()
            .map_err(|e| ApiError::Transport(e.to_string()))?;
        Ok(PreviewResult {
            employee_name: body.employee_name,
            employee_number: body.employee_number,
            badge_id: body.badge_id,
            current_state: CurrentState::parse(&body.current_state),
            next_action: Action::parse(&body.next_action),
            is_admin: body.is_admin,
        })
    }

    /// Phase 2: record the explicitly chosen event.
    ///
    /// # Errors
    ///
    /// Same status mapping as [`BackendClient::preview`].
    pub fn commit(&self, badge_id: &str, event_type: &str) -> Result<PunchResult, ApiError> {
        let url = format!("{}/api/v1/clock/nfc", self.base_url);
        let body = CommitRequest {
            badge_id,
            event_type,
            event_time: None,
            client_event_id: None,
        };
        let resp = self.send(&url, &body)?;
        let resp = self.check_status(resp)?;
        let body: CommitResponse = resp
            .json()
            .map_err(|e| ApiError::Transport(e.to_string()))?;
        Ok(PunchResult {
            employee_name: body.employee_name,
            employee_number: body.employee_number,
            action: Action::parse(&body.event_type),
            event_time: body.event_time,
            badge_id: badge_id.to_owned(),
            personal_messages: body.personal_messages,
        })
    }

    /// Aktive Rundmeldungen fuer den Ruhebildschirm (#189).
    ///
    /// # Errors
    /// [`ApiError::Transport`] bei Netzwerkfehlern; sonstige HTTP-Fehler werden
    /// vom Aufrufer (periodischer Poll) einfach ignoriert.
    pub fn kiosk_messages(&self) -> Result<Vec<String>, ApiError> {
        let url = format!("{}/api/v1/clock/kiosk-messages", self.base_url);
        let resp = self
            .http
            .get(&url)
            .header("X-Device-Api-Key", &self.device_key)
            .header("X-Tenant-Id", &self.tenant_id)
            .send()
            .map_err(|e| ApiError::Transport(e.to_string()))?;
        let resp = self.check_status(resp)?;
        let body: KioskMessagesResponse =
            resp.json().map_err(|e| ApiError::Transport(e.to_string()))?;
        Ok(body.messages)
    }

    /// Fetch the employee's live profile summary for the "Profil anschauen"
    /// screen. Non-recording.
    ///
    /// # Errors
    ///
    /// Same status mapping as [`BackendClient::preview`] (404 unknown badge,
    /// transport error on a network failure).
    pub fn profile(&self, badge_id: &str) -> Result<ProfileResult, ApiError> {
        let url = format!("{}/api/v1/clock/nfc/profile", self.base_url);
        let body = PreviewRequest { badge_id };
        let resp = self.send(&url, &body)?;
        let resp = self.check_status(resp)?;
        let body: ProfileResponse = resp
            .json()
            .map_err(|e| ApiError::Transport(e.to_string()))?;
        Ok(ProfileResult {
            employee_name: body.employee_name,
            current_state: CurrentState::parse(&body.current_state),
            today_start: body.today_start,
            today_end: body.today_end,
            today_work_minutes: body.today_work_minutes,
            today_break_minutes: body.today_break_minutes,
            today_overtime_minutes: body.today_overtime_minutes,
            konto_balance_minutes: body.arbeitszeitkonto_balance_minutes,
            contracted_hours_per_week: body.contracted_hours_per_week,
            vacation_remaining_days: body.vacation.map(|v| v.remaining_days),
        })
    }

    /// Submit a vacation request for the badge over the chosen date range.
    ///
    /// `date_from`/`date_to` are `YYYY-MM-DD` strings. The backend forces the
    /// type to VACATION, computes the working-day count and files it as
    /// SUBMITTED for manager approval.
    ///
    /// # Errors
    ///
    /// [`ApiError::InvalidTransition`] (HTTP 409) when an overlapping request
    /// already exists, [`ApiError::Http`] (422) for an invalid range, or the
    /// same mapping as [`BackendClient::preview`] otherwise.
    pub fn leave_request(
        &self,
        badge_id: &str,
        date_from: &str,
        date_to: &str,
    ) -> Result<LeaveResult, ApiError> {
        let url = format!("{}/api/v1/clock/nfc/leave-request", self.base_url);
        let body = LeaveRequestBody {
            badge_id,
            date_from,
            date_to,
        };
        let resp = self.send(&url, &body)?;
        let resp = self.check_status(resp)?;
        let body: LeaveResponse = resp
            .json()
            .map_err(|e| ApiError::Transport(e.to_string()))?;
        Ok(LeaveResult {
            status: body.status,
            days_count: body.days_count,
            vacation_remaining_days: body.vacation_remaining_days,
        })
    }

    /// Replay one queued offline punch.
    ///
    /// Entries with a known `event_type` go to `/nfc` (the explicit recording
    /// endpoint); entries queued while the backend was unreachable at tap time
    /// have no `event_type` and go to `/nfc/auto`, which derives Kommen/Gehen
    /// from the server-side state at replay. Both carry the original tap time
    /// and the idempotency key, so a retry can never double-book.
    ///
    /// # Errors
    ///
    /// [`ApiError::Transport`] means "backend still unreachable — keep the
    /// entry and try again later"; every other error is permanent for this
    /// entry (e.g. unknown badge, invalid transition after replay) and the
    /// caller should drop it.
    pub fn replay(&self, punch: &crate::queue::QueuedPunch) -> Result<(), ApiError> {
        let resp = match punch.event_type.as_deref() {
            Some(event_type) => {
                let url = format!("{}/api/v1/clock/nfc", self.base_url);
                let body = CommitRequest {
                    badge_id: &punch.badge_id,
                    event_type,
                    event_time: Some(&punch.event_time),
                    client_event_id: Some(&punch.client_event_id),
                };
                self.send(&url, &body)?
            }
            None => {
                let url = format!("{}/api/v1/clock/nfc/auto", self.base_url);
                let body = AutoRequest {
                    badge_id: &punch.badge_id,
                    event_time: &punch.event_time,
                    client_event_id: &punch.client_event_id,
                };
                self.send(&url, &body)?
            }
        };
        self.check_status(resp).map(|_| ())
    }

    /// Batch-register freshly personalized (or previously unresolved)
    /// token-ids into the tenant's scan pool (`POST /nfc/register-to-pool`).
    /// Used by the `sam_personalize` CLI (bead 4ty), the interactive
    /// kiosk's enroll path (`on_blank_personalized`), and its 404-backfill
    /// path (`dispatch_preview`) — bead rst.
    ///
    /// `uids` must be the same length as `token_ids` and parallel-indexed to
    /// it; an entry may be `None` when the chip UID is unavailable for that
    /// token. The backend upserts an existing pool entry's `chip_uid` when
    /// it was previously unset, reported back as `backfilled`.
    ///
    /// # Errors
    ///
    /// Same status mapping as [`BackendClient::preview`] — most notably,
    /// [`ApiError::Http`] with a 422 for a batch that fails the backend's
    /// `token_ids`/`uids` length/count/format validation.
    pub fn register_to_pool(
        &self,
        token_ids: &[String],
        uids: &[Option<String>],
    ) -> Result<RegisterToPoolResult, ApiError> {
        let request = self.build_register_to_pool_request(token_ids, uids)?;
        let resp = self
            .http
            .execute(request)
            .map_err(|e| ApiError::Transport(e.to_string()))?;
        let resp = self.check_status(resp)?;
        let body: RegisterToPoolResponse = resp
            .json()
            .map_err(|e| ApiError::Transport(e.to_string()))?;
        Ok(RegisterToPoolResult {
            pooled: body.pooled,
            skipped_existing: body.skipped_existing,
            skipped_assigned: body.skipped_assigned,
            backfilled: body.backfilled,
            pool_size: body.pool_size,
        })
    }

    /// Pure(ish) request construction for [`BackendClient::register_to_pool`],
    /// split out so it can be unit-tested (URL, headers, JSON body) without
    /// ever calling `.send()`/hitting the network — `reqwest::RequestBuilder::build()`
    /// only assembles the [`reqwest::blocking::Request`] object, it performs no I/O.
    fn build_register_to_pool_request(
        &self,
        token_ids: &[String],
        uids: &[Option<String>],
    ) -> Result<reqwest::blocking::Request, ApiError> {
        let url = format!("{}/api/v1/clock/nfc/register-to-pool", self.base_url);
        let body = RegisterToPoolBody { token_ids, uids };
        self.http
            .post(&url)
            .header("X-Device-Api-Key", &self.device_key)
            .header("X-Tenant-Id", &self.tenant_id)
            .json(&body)
            .build()
            .map_err(|e| ApiError::Transport(e.to_string()))
    }

    /// Batch-flag freshly personalized token-ids as admin fobs
    /// (`POST /nfc/register-admin-cards`) — gates the wall terminal's
    /// personalize mode. Used by the `sam_personalize --admin` CLI and by
    /// the terminal's own personalize mode's admin-minting path (if wired
    /// up) — never by the interactive kiosk punch flow.
    ///
    /// # Errors
    ///
    /// Same status mapping as [`BackendClient::preview`].
    pub fn register_admin_cards(
        &self,
        token_ids: &[String],
    ) -> Result<RegisterAdminCardsResult, ApiError> {
        let request = self.build_register_admin_cards_request(token_ids)?;
        let resp = self
            .http
            .execute(request)
            .map_err(|e| ApiError::Transport(e.to_string()))?;
        let resp = self.check_status(resp)?;
        let body: RegisterAdminCardsResponse = resp
            .json()
            .map_err(|e| ApiError::Transport(e.to_string()))?;
        Ok(RegisterAdminCardsResult {
            added: body.added,
            already_admin: body.already_admin,
            admin_count: body.admin_count,
        })
    }

    /// Pure(ish) request construction for
    /// [`BackendClient::register_admin_cards`] — same rationale as
    /// [`BackendClient::build_register_to_pool_request`] (unit-testable
    /// without network I/O).
    fn build_register_admin_cards_request(
        &self,
        token_ids: &[String],
    ) -> Result<reqwest::blocking::Request, ApiError> {
        let url = format!("{}/api/v1/clock/nfc/register-admin-cards", self.base_url);
        let body = RegisterAdminCardsBody { token_ids };
        self.http
            .post(&url)
            .header("X-Device-Api-Key", &self.device_key)
            .header("X-Tenant-Id", &self.tenant_id)
            .json(&body)
            .build()
            .map_err(|e| ApiError::Transport(e.to_string()))
    }

    /// Issue a POST with the device auth headers and a JSON body.
    fn send<B: Serialize>(
        &self,
        url: &str,
        body: &B,
    ) -> Result<reqwest::blocking::Response, ApiError> {
        let mut last_err = String::new();
        for attempt in 1..=2 {
            let result = self
                .http
                .post(url)
                .header("X-Device-Api-Key", &self.device_key)
                .header("X-Tenant-Id", &self.tenant_id)
                .json(body)
                .send();
            match result {
                Ok(resp) => {
                    eprintln!(
                        "[api] POST {url} -> HTTP {} (Versuch {attempt})",
                        resp.status().as_u16()
                    );
                    return Ok(resp);
                }
                Err(e) => {
                    last_err = e.to_string();
                    eprintln!("[api] POST {url} FEHLER (Versuch {attempt}/2): {last_err}");
                }
            }
        }
        Err(ApiError::Transport(last_err))
    }

    /// Map a non-2xx response onto a typed [`ApiError`], extracting the
    /// FastAPI `detail` field where present.
    fn check_status(
        &self,
        resp: reqwest::blocking::Response,
    ) -> Result<reqwest::blocking::Response, ApiError> {
        let status = resp.status();
        if status.is_success() {
            return Ok(resp);
        }

        let code = status.as_u16();
        let detail = extract_detail(resp, status);

        Err(match code {
            404 => ApiError::UnknownBadge(detail),
            409 => ApiError::InvalidTransition(detail),
            429 => ApiError::RateLimited(detail),
            _ => ApiError::Http {
                status: code,
                detail,
            },
        })
    }
}

/// Pull the `detail` string out of a FastAPI error body, falling back to a
/// generic message keyed off the status code.
fn extract_detail(resp: reqwest::blocking::Response, status: reqwest::StatusCode) -> String {
    match resp.json::<DetailBody>() {
        Ok(DetailBody {
            detail: Some(value),
        }) => value.into_text(),
        _ => format!("Unerwarteter Fehler ({})", status.as_u16()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn transport_and_5xx_and_rate_limited_are_transient() {
        assert!(ApiError::Transport("connection reset".to_owned()).is_transient());
        assert!(ApiError::RateLimited("too fast".to_owned()).is_transient());
        assert!(ApiError::Http {
            status: 500,
            detail: "internal error".to_owned(),
        }
        .is_transient());
        assert!(ApiError::Http {
            status: 503,
            detail: "service unavailable".to_owned(),
        }
        .is_transient());
    }

    #[test]
    fn business_rejections_are_permanent() {
        assert!(!ApiError::UnknownBadge("no such badge".to_owned()).is_transient());
        assert!(!ApiError::InvalidTransition("already clocked in".to_owned()).is_transient());
        assert!(!ApiError::Http {
            status: 422,
            detail: "validation failed".to_owned(),
        }
        .is_transient());
        assert!(!ApiError::Http {
            status: 400,
            detail: "bad request".to_owned(),
        }
        .is_transient());
    }

    #[test]
    fn parses_lowercase_backend_actions() {
        assert_eq!(Action::parse("clock_in"), Action::PunchIn);
        assert_eq!(Action::parse("clock_out"), Action::PunchOut);
        assert_eq!(Action::parse("break_start"), Action::BreakStart);
        assert_eq!(Action::parse("break_end"), Action::BreakEnd);
    }

    #[test]
    fn parses_uppercase_punch_actions() {
        assert_eq!(Action::parse("PUNCH_IN"), Action::PunchIn);
        assert_eq!(Action::parse("PUNCH_OUT"), Action::PunchOut);
        assert_eq!(Action::parse("BREAK_START"), Action::BreakStart);
        assert_eq!(Action::parse("BREAK_END"), Action::BreakEnd);
    }

    #[test]
    fn unknown_action_is_preserved() {
        assert_eq!(
            Action::parse("teleport"),
            Action::Other("teleport".to_owned())
        );
    }

    #[test]
    fn event_type_round_trips() {
        assert_eq!(Action::PunchIn.event_type(), Some("PUNCH_IN"));
        assert_eq!(Action::PunchOut.event_type(), Some("PUNCH_OUT"));
        assert_eq!(Action::BreakStart.event_type(), Some("BREAK_START"));
        assert_eq!(Action::BreakEnd.event_type(), Some("BREAK_END"));
        assert_eq!(Action::Other("x".to_owned()).event_type(), None);
    }

    #[test]
    fn german_labels_are_mapped() {
        assert_eq!(Action::PunchIn.label_de(), "Kommen erfasst");
        assert_eq!(Action::PunchOut.label_de(), "Gehen erfasst");
        assert_eq!(Action::BreakStart.label_de(), "Pause begonnen");
        assert_eq!(Action::BreakEnd.label_de(), "Pause beendet");
    }

    #[test]
    fn german_headings_use_first_name() {
        assert_eq!(Action::PunchIn.heading_de("Anna"), "Willkommen, Anna!");
        assert_eq!(
            Action::PunchOut.heading_de("Anna"),
            "Schönen Feierabend, Anna!"
        );
        assert_eq!(Action::BreakStart.heading_de("Anna"), "Gute Pause, Anna!");
        assert_eq!(
            Action::BreakEnd.heading_de("Anna"),
            "Willkommen zurück, Anna!"
        );
    }

    #[test]
    fn parses_current_states() {
        assert_eq!(CurrentState::parse("clocked_out"), CurrentState::ClockedOut);
        assert_eq!(CurrentState::parse("clocked_in"), CurrentState::ClockedIn);
        assert_eq!(CurrentState::parse("on_break"), CurrentState::OnBreak);
        assert_eq!(
            CurrentState::parse("frozen"),
            CurrentState::Other("frozen".to_owned())
        );
    }

    #[test]
    fn first_name_takes_first_token() {
        assert_eq!(first_name("Anna Müller"), "Anna");
        assert_eq!(first_name("Bob"), "Bob");
        assert_eq!(first_name("Erika Demo (Demo)"), "Erika");
        assert_eq!(first_name("  Carla   Weiss "), "Carla");
        assert_eq!(first_name(""), "");
    }

    // -------------------------------------------------------------
    // register_to_pool request construction (bead 4ty) — pure request
    // building via `RequestBuilder::build()`, no network I/O, no live
    // server needed.
    // -------------------------------------------------------------

    fn test_client() -> BackendClient {
        let config = crate::config::Config {
            backend_url: "https://kiosk.example.test".to_owned(),
            tenant_id: "11111111-1111-1111-1111-111111111111".to_owned(),
            device_key: "test-device-key".to_owned(),
            device_id: "TERMINAL-TEST".to_owned(),
            reader: crate::config::ReaderKind::Mock,
            sam_aid: [0u8; 3],
            sam_app_key_no: 0,
            sam_key_entry_no: 0,
            sam_key_entry_ver: 0,
            sam_file_no: 0,
            sam_token_id_len: 16,
            sam_reader_substr: "SAM".to_owned(),
            picc_reader_substr: "PICC".to_owned(),
        };
        BackendClient::new(&config).unwrap()
    }

    #[test]
    fn register_to_pool_request_has_correct_url_and_method() {
        let client = test_client();
        let token_ids = vec!["abc123".to_owned()];
        let uids = vec![Some("aabbccddeeff11".to_owned())];
        let request = client
            .build_register_to_pool_request(&token_ids, &uids)
            .unwrap();

        assert_eq!(request.method(), reqwest::Method::POST);
        assert_eq!(
            request.url().as_str(),
            "https://kiosk.example.test/api/v1/clock/nfc/register-to-pool"
        );
    }

    #[test]
    fn register_to_pool_request_has_device_auth_headers() {
        let client = test_client();
        let token_ids = vec!["abc123".to_owned()];
        let uids = vec![Some("aabbccddeeff11".to_owned())];
        let request = client
            .build_register_to_pool_request(&token_ids, &uids)
            .unwrap();

        assert_eq!(
            request.headers().get("X-Device-Api-Key").unwrap(),
            "test-device-key"
        );
        assert_eq!(
            request.headers().get("X-Tenant-Id").unwrap(),
            "11111111-1111-1111-1111-111111111111"
        );
    }

    #[test]
    fn register_to_pool_request_body_is_json_token_ids_and_uids_arrays() {
        let client = test_client();
        let token_ids = vec!["tok-a".to_owned(), "tok-b".to_owned(), "tok-c".to_owned()];
        let uids = vec![
            Some("aabbccddeeff11".to_owned()),
            None,
            Some("112233445566aa".to_owned()),
        ];
        let request = client
            .build_register_to_pool_request(&token_ids, &uids)
            .unwrap();

        let body_bytes = request.body().unwrap().as_bytes().unwrap();
        let parsed: serde_json::Value = serde_json::from_slice(body_bytes).unwrap();
        assert_eq!(
            parsed,
            serde_json::json!({
                "token_ids": ["tok-a", "tok-b", "tok-c"],
                "uids": ["aabbccddeeff11", null, "112233445566aa"],
            })
        );
    }

    #[test]
    fn register_to_pool_request_content_type_is_json() {
        let client = test_client();
        let token_ids = vec!["abc123".to_owned()];
        let uids = vec![Some("aabbccddeeff11".to_owned())];
        let request = client
            .build_register_to_pool_request(&token_ids, &uids)
            .unwrap();

        assert_eq!(
            request
                .headers()
                .get(reqwest::header::CONTENT_TYPE)
                .unwrap(),
            "application/json"
        );
    }

    #[test]
    fn register_to_pool_response_backfilled_defaults_zero_when_absent() {
        let json = serde_json::json!({
            "pooled": 1,
            "skipped_existing": 0,
            "skipped_assigned": 0,
            "pool_size": 5,
        });
        let parsed: RegisterToPoolResponse = serde_json::from_value(json).unwrap();
        assert_eq!(parsed.backfilled, 0);
    }

    #[test]
    fn register_to_pool_response_backfilled_parses_when_present() {
        let json = serde_json::json!({
            "pooled": 0,
            "skipped_existing": 1,
            "skipped_assigned": 0,
            "backfilled": 1,
            "pool_size": 5,
        });
        let parsed: RegisterToPoolResponse = serde_json::from_value(json).unwrap();
        assert_eq!(parsed.backfilled, 1);
    }

    // -------------------------------------------------------------
    // register_admin_cards request construction (terminal personalize
    // mode) — same pure request-building approach as register_to_pool.
    // -------------------------------------------------------------

    #[test]
    fn register_admin_cards_request_has_correct_url_and_method() {
        let client = test_client();
        let token_ids = vec!["admin-tok-1".to_owned()];
        let request = client
            .build_register_admin_cards_request(&token_ids)
            .unwrap();

        assert_eq!(request.method(), reqwest::Method::POST);
        assert_eq!(
            request.url().as_str(),
            "https://kiosk.example.test/api/v1/clock/nfc/register-admin-cards"
        );
    }

    #[test]
    fn register_admin_cards_request_has_device_auth_headers() {
        let client = test_client();
        let token_ids = vec!["admin-tok-1".to_owned()];
        let request = client
            .build_register_admin_cards_request(&token_ids)
            .unwrap();

        assert_eq!(
            request.headers().get("X-Device-Api-Key").unwrap(),
            "test-device-key"
        );
        assert_eq!(
            request.headers().get("X-Tenant-Id").unwrap(),
            "11111111-1111-1111-1111-111111111111"
        );
    }

    #[test]
    fn register_admin_cards_request_body_is_json_token_ids_array() {
        let client = test_client();
        let token_ids = vec!["admin-a".to_owned(), "admin-b".to_owned()];
        let request = client
            .build_register_admin_cards_request(&token_ids)
            .unwrap();

        let body_bytes = request.body().unwrap().as_bytes().unwrap();
        let parsed: serde_json::Value = serde_json::from_slice(body_bytes).unwrap();
        assert_eq!(
            parsed,
            serde_json::json!({ "token_ids": ["admin-a", "admin-b"] })
        );
    }

    // -------------------------------------------------------------
    // PreviewResponse.is_admin defaulting
    // -------------------------------------------------------------

    #[test]
    fn preview_response_is_admin_defaults_false_when_absent() {
        let json = serde_json::json!({
            "employee_name": "Anna Müller",
            "employee_number": "E1",
            "badge_id": "NFC005",
            "current_state": "clocked_out",
            "next_action": "clock_in",
        });
        let parsed: PreviewResponse = serde_json::from_value(json).unwrap();
        assert!(!parsed.is_admin);
    }

    #[test]
    fn preview_response_is_admin_true_when_present() {
        let json = serde_json::json!({
            "employee_name": "",
            "employee_number": "",
            "badge_id": "04ADMIN",
            "current_state": "clocked_out",
            "next_action": "clock_in",
            "is_admin": true,
        });
        let parsed: PreviewResponse = serde_json::from_value(json).unwrap();
        assert!(parsed.is_admin);
    }

    #[test]
    fn strukturierter_fehler_wird_deutsch_statt_json() {
        // Terminal zeigte frueher das rohe JSON-Objekt (#185).
        let json = serde_json::json!({
            "code": "invalid_punch_state",
            "message": "Cannot record BREAK_START: current state is 'PUNCH_OUT'."
        });
        let detail: DetailValue = serde_json::from_value(json).unwrap();
        assert_eq!(
            detail.into_text(),
            "Buchung nicht möglich – bitte zuerst einstempeln."
        );
    }

    #[test]
    fn unbekannter_code_nutzt_message() {
        let json = serde_json::json!({"code": "irgendwas", "message": "Klartext vom Server"});
        let detail: DetailValue = serde_json::from_value(json).unwrap();
        assert_eq!(detail.into_text(), "Klartext vom Server");
    }

    #[test]
    fn einfacher_text_bleibt_unveraendert() {
        let detail: DetailValue = serde_json::from_value(serde_json::json!("Nur Text")).unwrap();
        assert_eq!(detail.into_text(), "Nur Text");
    }
}
