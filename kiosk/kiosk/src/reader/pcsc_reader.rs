//! Real PC/SC card reader for the ACR1252U-M1 (and similar dual-slot PICC +
//! SAM readers) — SAM-mediated DESFire+SAM EV2 punch authentication (bead
//! bi0).
//!
//! Only compiled with the `real-reader` feature, which pulls in the `pcsc`
//! crate (and the system `libpcsclite`). On each card presence, the loop:
//!
//! 1. Connects to the PICC slot and reads the card's UID — used **only** as
//!    the SAM diversification input, never emitted as an identity (see
//!    [`super::sam::punch`]'s module doc).
//! 2. Connects to the SAM slot and runs
//!    [`super::sam::punch::authenticate_and_read_token_id`] (fetches the
//!    card's diversified per-fob app key OFFLINE via `SAM_DumpSecretKey`,
//!    then authenticates + reads the stable token-id — written by
//!    personalization, bead 4ty — entirely in software; bead bi0 rework
//!    2026-07-15, see `punch`'s module doc for why the earlier
//!    SAM-mediated relay design was abandoned).
//! 3. On success, emits [`CardEvent::Detected`] with the **authenticated
//!    token-id** (hex) — never the UID.
//! 4. **FAIL-CLOSED (bead cqo)**: on ANY failure at any step — no SAM
//!    reader present, a non-DESFire/foreign/blank/cloned card, an EV2 auth
//!    rejection, a select/read rejection, any transport error — NO
//!    `CardEvent::Detected` is emitted. The tap is silently rejected
//!    (logged to stderr for operator visibility). There is **no** fallback
//!    path that emits the bare card UID as a badge id — the previous
//!    UID-only prototype behavior has been fully removed from this file.
//!
//! **Terminal personalize mode** (admin-fob-gated batch fob
//! personalization, bead c17): when `personalize_flag` (shared with the UI
//! thread — see `main.rs::sync_personalize_flag`) is set at the moment an
//! auth failure occurs, the loop additionally attempts to personalize the
//! tapped card as a *blank* DESFire fob (reusing
//! [`super::sam::personalize::personalize_fob`] unchanged — same
//! hardware-verified crypto core as the `sam_personalize` SSH CLI). Outside
//! personalize mode this additional attempt never happens — the fail-closed
//! behavior above is completely unchanged. See
//! [`authenticate_tap`]/[`TapOutcome`].

use std::ffi::CString;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::Sender;
use std::sync::Arc;
use std::time::Duration;

use pcsc::{Context, ReaderState, Scope, State, PNP_NOTIFICATION};

use super::sam::personalize::{self, PersonalizeConfig};
use super::sam::punch::{
    authenticate_and_read_token_id, generate_punch_challenge, PunchAuthConfig,
};
use super::sam::transport::{find_reader_by_name_substring, PiccPcscTransport, SamPcscTransport};
use super::{CardEvent, CardReader};

/// Outcome of one [`authenticate_tap`] attempt — the card-level result,
/// distinct from a transport/infra `Err` (see that function's doc comment).
enum TapOutcome {
    /// Normal SAM/EV2 authentication succeeded (employee or admin fob —
    /// undistinguished here; the backend's `is_admin` preview flag makes
    /// that call, see `main.rs::dispatch_preview`). Carries both the
    /// authenticated token-id and the card's chip UID (hex, bead rst).
    Detected { token: String, uid: String },
    /// Terminal personalize mode only: the card failed normal auth but was
    /// successfully personalized as a blank DESFire fob. Carries both the
    /// freshly written token-id and the fob's chip UID (hex, bead rst).
    Personalized { token: String, uid: String },
    /// Terminal personalize mode only: the card failed BOTH normal auth AND
    /// blank-fob personalization (foreign/non-DESFire/damaged card).
    Rejected,
}

/// PC/SC-backed reader. Construction is infallible; failures surface when the
/// loop runs (so a missing daemon/reader/SAM doesn't prevent process startup).
pub struct PcscReader {
    /// Case-insensitive substring identifying the SAM-slot PC/SC reader
    /// (e.g. `"SAM"`), see `config::Config::sam_reader_substr`.
    sam_reader_substr: String,
    /// Case-insensitive substring identifying the PICC-slot (contactless)
    /// PC/SC reader (e.g. `"PICC"`), see `config::Config::picc_reader_substr`.
    picc_reader_substr: String,
    /// AID / key-entry / file-no / token-id-len parameters — must match the
    /// values used at personalization time (bead 4ty); see
    /// [`PunchAuthConfig`]'s doc comment.
    auth_cfg: PunchAuthConfig,
    /// Mirrors terminal personalize mode (see `main.rs::sync_personalize_flag`)
    /// — read on every auth failure to decide whether to attempt blank-fob
    /// personalization.
    personalize_flag: Arc<AtomicBool>,
    /// Parameters for personalizing a blank fob at the terminal — reuses the
    /// same AID/company-key-entry configuration the punch path already
    /// requires at startup (`sam_aid`, `sam_key_entry_no`/`_ver`); the
    /// 2026-07-15 offline-personalization rework authenticates a blank
    /// fob's factory-default key entirely in software, so no *additional*
    /// SAM key entry is needed to enable this path — personalize mode is
    /// always usable once the terminal has a working PC/SC/SAM setup at
    /// all.
    personalize_cfg: PersonalizeConfig,
}

impl PcscReader {
    /// Construct a new PC/SC reader handle.
    #[must_use]
    pub fn new(
        sam_reader_substr: String,
        picc_reader_substr: String,
        auth_cfg: PunchAuthConfig,
        personalize_flag: Arc<AtomicBool>,
        personalize_cfg: PersonalizeConfig,
    ) -> Self {
        Self {
            sam_reader_substr,
            picc_reader_substr,
            auth_cfg,
            personalize_flag,
            personalize_cfg,
        }
    }
}

impl CardReader for PcscReader {
    fn run(self: Box<Self>, tx: Sender<CardEvent>) {
        // Establish a context; on failure there is nothing the kiosk can do but
        // report and stop the reader thread.
        let ctx = match Context::establish(Scope::User) {
            Ok(ctx) => ctx,
            Err(err) => {
                eprintln!("PC/SC: cannot establish context: {err}");
                return;
            }
        };

        loop {
            let picc_reader_name =
                match find_reader_by_name_substring(&ctx, &self.picc_reader_substr) {
                    Some(name) => name,
                    None => {
                        // No PICC reader yet — wait briefly and retry, exiting if
                        // the UI side has gone away.
                        if tx.send(CardEvent::Removed).is_err() {
                            return;
                        }
                        std::thread::sleep(Duration::from_millis(500));
                        continue;
                    }
                };

            // Fail-closed at the reader-discovery level too: refuse to run
            // the watch loop at all while the SAM slot is unavailable,
            // rather than silently degrading to "detect presence but every
            // tap rejects" (which would still be safe, but noisier and
            // slower to notice than refusing to loop).
            if find_reader_by_name_substring(&ctx, &self.sam_reader_substr).is_none() {
                eprintln!(
                    "PC/SC: no SAM reader matching {:?} found — refusing all taps until the SAM \
                     is present (fail-closed, bead cqo)",
                    self.sam_reader_substr
                );
                if tx.send(CardEvent::Removed).is_err() {
                    return;
                }
                std::thread::sleep(Duration::from_millis(1000));
                continue;
            }

            if let Err(err) = watch_reader(
                &ctx,
                &picc_reader_name,
                &self.sam_reader_substr,
                &self.auth_cfg,
                &self.personalize_flag,
                &self.personalize_cfg,
                &tx,
            ) {
                // Reader disappeared / transient error: report and re-discover.
                eprintln!("PC/SC: reader error on {picc_reader_name:?}: {err}");
                if tx.send(CardEvent::Removed).is_err() {
                    return;
                }
                std::thread::sleep(Duration::from_millis(500));
            }
        }
    }
}

/// Block on the PICC reader, emitting Detected/Removed/Personalized/
/// PersonalizeRejected across card insertion/removal cycles. Returns `Ok(())`
/// only when the channel receiver is gone; otherwise an error means the
/// caller should re-discover the reader.
///
/// A `Detected` event is emitted **only** after a full successful
/// authenticate-and-read (see [`authenticate_tap`]); any transport/infra
/// failure there is logged and no event is emitted for that tap at all —
/// unchanged, fail-closed (bead cqo). `Personalized`/`PersonalizeRejected`
/// are only ever emitted while `personalize_flag` was set at tap time.
#[allow(clippy::too_many_arguments)]
fn watch_reader(
    ctx: &Context,
    picc_reader_name: &CString,
    sam_reader_substr: &str,
    auth_cfg: &PunchAuthConfig,
    personalize_flag: &AtomicBool,
    personalize_cfg: &PersonalizeConfig,
    tx: &Sender<CardEvent>,
) -> Result<(), pcsc::Error> {
    // Track PnP plus our target reader so changes wake `get_status_change`.
    let mut states = [
        ReaderState::new(PNP_NOTIFICATION(), State::UNAWARE),
        ReaderState::new(picc_reader_name.clone(), State::UNAWARE),
    ];

    let mut card_present = false;

    loop {
        // Block until any monitored reader changes state.
        ctx.get_status_change(None, &mut states)?;

        for rs in &mut states {
            rs.sync_current_state();
        }

        let target = &states[1];
        let event_state = target.event_state();

        // Reader vanished — let the caller re-discover.
        if event_state.contains(State::UNKNOWN) || event_state.contains(State::IGNORE) {
            return Err(pcsc::Error::ReaderUnavailable);
        }

        let now_present = event_state.contains(State::PRESENT);

        if now_present && !card_present {
            card_present = true;
            // Snapshot the flag once per tap — a toggle mid-authentication
            // (unlikely given the sub-second handshake, but possible) must
            // not change behavior partway through a single tap.
            let personalizing = personalize_flag.load(Ordering::Relaxed);
            match authenticate_tap(
                ctx,
                picc_reader_name,
                sam_reader_substr,
                auth_cfg,
                personalizing,
                personalize_cfg,
            ) {
                Ok(TapOutcome::Detected { token, uid }) => {
                    if tx.send(CardEvent::Detected { token, uid }).is_err() {
                        return Ok(());
                    }
                }
                Ok(TapOutcome::Personalized { token, uid }) => {
                    if tx.send(CardEvent::Personalized { token, uid }).is_err() {
                        return Ok(());
                    }
                }
                Ok(TapOutcome::Rejected) => {
                    // Only surface as an event while personalize mode is on
                    // — outside it, a rejection stays silent (fail-closed,
                    // bead cqo, unchanged).
                    if personalizing {
                        if tx.send(CardEvent::PersonalizeRejected).is_err() {
                            return Ok(());
                        }
                    } else {
                        eprintln!("PC/SC: punch authentication rejected");
                    }
                }
                Err(msg) => {
                    // Transport/infra failure (reader missing, connect
                    // failure, UID read failure): log only, NEVER emit an
                    // event — fail-closed (bead cqo), unchanged in both
                    // modes. Never falls back to emitting the card's UID.
                    eprintln!("PC/SC: punch authentication rejected: {msg}");
                }
            }
        } else if !now_present && card_present {
            card_present = false;
            if tx.send(CardEvent::Removed).is_err() {
                return Ok(());
            }
        }
    }
}

/// Run the full SAM-mediated authenticate-and-read for one tap: open the SAM
/// and PICC PC/SC connections, read the UID (diversification input only),
/// then delegate to [`authenticate_and_read_token_id`] for the EV2
/// authentication and token-id read.
///
/// On an auth failure, when `personalizing` is true, additionally attempts
/// to personalize the card as a blank DESFire fob (terminal personalize
/// mode) — reusing [`personalize::personalize_fob`] unchanged, the same
/// hardware-verified crypto core the `sam_personalize` SSH CLI uses. When
/// `personalizing` is false, an auth failure is returned as `Err` exactly as
/// before this feature existed (no personalize attempt, no
/// `TapOutcome::Rejected`).
///
/// Returns `Err` only for a transport/infra failure (SAM reader missing,
/// PICC/SAM connect failure, UID read failure) — the caller must NOT emit
/// any event for those (fail-closed, bead cqo, unchanged in both modes). A
/// card-level rejection (bad key, foreign card, failed personalization) is
/// `Ok(TapOutcome::Rejected)`, distinguished so the caller can decide whether
/// to surface it as [`CardEvent::PersonalizeRejected`] (only while
/// personalizing).
///
/// HARDWARE-VERIFY: on the personalize fallback path, a *second* fresh
/// SAM+PICC connection is opened for the `personalize_fob` attempt (the
/// first pair's session state was already spent on the failed
/// `authenticate_and_read_token_id` call) — whether re-selecting/
/// re-authenticating against the SAME physical tap on freshly (re-)opened
/// `pcsc::Card` handles, back-to-back with no removal in between, behaves
/// identically to a fresh tap has only been exercised via the punch path on
/// real SAM AV3 + DESFire EV2 hardware, not this specific back-to-back
/// personalize-retry sequence.
#[allow(clippy::too_many_arguments)]
fn authenticate_tap(
    ctx: &Context,
    picc_reader_name: &CString,
    sam_reader_substr: &str,
    auth_cfg: &PunchAuthConfig,
    personalizing: bool,
    personalize_cfg: &PersonalizeConfig,
) -> Result<TapOutcome, String> {
    let sam_reader_name = find_reader_by_name_substring(ctx, sam_reader_substr)
        .ok_or_else(|| format!("no PC/SC reader matching {sam_reader_substr:?} (SAM) found"))?;

    let mut sam_transport = SamPcscTransport::open_named(ctx, &sam_reader_name)
        .map_err(|e| format!("opening SAM reader {sam_reader_name:?}: {e}"))?;

    let mut picc_transport = PiccPcscTransport::open_named(ctx, picc_reader_name)
        .map_err(|e| format!("opening PICC reader {picc_reader_name:?}: {e}"))?;

    // UID is used ONLY as the SAM diversification input below — never
    // returned, logged as an identity, or emitted as a CardEvent.
    let uid = picc_transport
        .read_uid()
        .map_err(|e| format!("reading UID: {e}"))?;

    let challenge = generate_punch_challenge().map_err(|e| e.to_string())?;
    let auth_err = match authenticate_and_read_token_id(
        &mut sam_transport,
        &mut picc_transport,
        uid,
        auth_cfg,
        challenge,
    ) {
        Ok(token_id_hex) => {
            return Ok(TapOutcome::Detected {
                token: token_id_hex,
                uid: hex::encode(uid),
            })
        }
        Err(e) => e,
    };

    if !personalizing {
        // Fail-closed, unchanged: no personalize attempt outside personalize
        // mode — the caller treats this exactly as before this feature
        // existed (log only, no event at all).
        return Err(auth_err.to_string());
    }

    // Re-open fresh SAM+PICC connections for the personalize attempt — see
    // this function's HARDWARE-VERIFY doc comment.
    let mut sam_transport2 = SamPcscTransport::open_named(ctx, &sam_reader_name)
        .map_err(|e| format!("opening SAM reader {sam_reader_name:?} (personalize retry): {e}"))?;
    let mut picc_transport2 =
        PiccPcscTransport::open_named(ctx, picc_reader_name).map_err(|e| {
            format!("opening PICC reader {picc_reader_name:?} (personalize retry): {e}")
        })?;
    let uid2 = picc_transport2
        .read_uid()
        .map_err(|e| format!("reading UID (personalize retry): {e}"))?;

    let token_id = personalize::generate_token_id().map_err(|e| e.to_string())?;
    let personalize_challenge =
        personalize::generate_personalize_challenge().map_err(|e| e.to_string())?;
    match personalize::personalize_fob(
        &mut sam_transport2,
        &mut picc_transport2,
        uid2,
        personalize_cfg,
        token_id,
        personalize_challenge,
    ) {
        Ok(result) => Ok(TapOutcome::Personalized {
            token: result.token_id_hex,
            uid: result.uid_hex,
        }),
        Err(personalize_err) => {
            eprintln!(
                "[personalize] blank-fob personalization also failed after normal auth failed \
                 (auth: {auth_err}; personalize: {personalize_err}) — treating as a foreign/\
                 rejected card"
            );
            Ok(TapOutcome::Rejected)
        }
    }
}
