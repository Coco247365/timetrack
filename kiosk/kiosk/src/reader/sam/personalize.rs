//! DESFire fob personalization (bead 4ty): the per-fob procedure run at the
//! kiosk terminal (via the `sam_personalize` SSH CLI,
//! `src/bin/sam_personalize.rs`) to turn a blank DESFire EV2 card into a
//! badge the backend can trust.
//!
//! This module is transport-agnostic and pure logic (testable against
//! [`super::mock::MockTransport`] for both the SAM and PICC roles — no
//! hardware needed).
//!
//! **Scope of this module**: personalizing one fob at a time (create the
//! application, authenticate to its freshly-created default app key,
//! diversify+write the app key, create the token-id file, write a fresh
//! random token-id). Batch looping across N fobs and the backend
//! `register-to-pool` HTTP call live in the CLI
//! (`src/bin/sam_personalize.rs`), which has no crypto logic of its own —
//! it is a thin shell around this module plus [`crate::api`].
//!
//! **Fail-closed, always**: every step propagates [`SamError`] via `?`.
//! There is no fallback path — if any step fails, the fob's `token_id` is
//! never registered into the backend pool, and the caller must not treat
//! that fob as personalized. The card itself may be left in a partially
//! personalized state (DESFire has no cross-command transaction spanning
//! `CreateApplication`..`WriteData`) — this is an unavoidable hardware
//! limitation, not a software gap; the CLI reports every such fob
//! explicitly rather than silently dropping it.
//!
//! ## Incident 2026-07-15: PICC-master-key auth was wrong from the start
//!
//! This module originally opened with `SAM_AuthenticatePICC` AES
//! `AuthenticateEV2First` (`0x71`) against the card's **PICC master key**
//! (key 0 at the master/PICC level, AID `000000`), on the assumption that a
//! factory-fresh card ships that key as all-zero AES. Tested against a real
//! DESFire fob on an ACR1252U for the first time this day, that assumption
//! turned out to be wrong on two counts, both confirmed via live APDU
//! probes:
//!
//! 1. A factory DESFire's PICC master key is **2K3DES**, not AES —
//!    `AuthenticateAES` (`0xAA`) and `AuthenticateEV2First` (`0x71`) both
//!    got `91 AE` (rejected); `AuthenticateISO` (`0x1A`) and legacy `0x0A`
//!    got an 8-byte challenge + `91 AF` (accepted). So step 1 could never
//!    have succeeded against a real factory card, AES key entry or not.
//! 2. `CreateApplication` needs **no authentication at all** on a factory
//!    card — `91 00` (success) with zero prior auth, confirmed live by
//!    creating a throwaway app (`F5F5F5`).
//!
//! The fix authenticated at the **application** level instead, against a
//! freshly-created app's own key 0 — which genuinely IS all-zero AES on
//! creation, *provided the app is actually AES-typed* (see
//! [`apdu::KEY_SETTINGS2_AES`]). That first fix still relayed the
//! authentication/key-change through the SAM (`SAM_AuthenticatePICC` +
//! `SAM_ChangeKeyPICC`) — see the next incident note for why that turned
//! out to be the wrong mechanism too.
//!
//! ## Rework 2026-07-15: `SAM_AuthenticatePICC` relay abandoned for the
//! offline (`SAM_DumpSecretKey`) model
//!
//! `SAM_AuthenticatePICC` (INS `0x0A`) — the "live relay" model where the
//! SAM does the DESFire crypto and the host just shuttles bytes between the
//! SAM slot and the PICC slot — failed on real hardware (`SAM` returned
//! `6A84` on the pass-1 relay). Root cause (confirmed via web research and
//! the local `liblogicalaccess` reference,
//! `~/.zeiterfassung-ops/sam-refs/samav2iso7816commands.cpp`): live
//! `SAM_AuthenticatePICC` relay is not the intended personalization
//! mechanism at all — the correct model is OFFLINE: the SAM diversifies a
//! key and hands the RAW key material to the host via `SAM_DumpSecretKey`
//! (INS `0xD6`), and the HOST does every byte of the DESFire crypto in
//! software from there. This module now:
//!
//! 1. Authenticates to the app's factory all-zero AES key 0 **entirely in
//!    software** ([`flows::authenticate_picc_ev2_software`]) — no SAM
//!    involvement at all for this step, since the key is publicly known
//!    (it's the DESFire spec's own factory default).
//! 2. Fetches the diversified company key from the SAM via
//!    [`flows::dump_secret_key`] (`SAM_DumpSecretKey`, Full-protection
//!    wrapped under the still-needed [`flows::authenticate_host`] session).
//! 3. Builds and encrypts the native `ChangeKey` cryptogram **in software**
//!    ([`crypto::build_change_key_self_cryptogram`],
//!    [`crypto::DesfireEv2Session`]) and sends it directly to the card.
//! 4. Creates the token-id file and writes the token-id, both **directly to
//!    the card** — `SAM_Apply_SM`/`SAM_Remove_SM` ([`flows::send_via_sm`])
//!    can no longer be used from step 1 onward, because the SAM never
//!    establishes a tracked PICC session in this design (see step 6's doc
//!    comment below for why).
//!
//! The SAM host session ([`flows::authenticate_host`]) is STILL needed —
//! `SAM_DumpSecretKey` requires it — but `SAM_AuthenticatePICC` and
//! `SAM_ChangeKeyPICC` are gone from this flow (their builders/parsers are
//! kept in `apdu.rs`/`flows.rs`, marked SUPERSEDED, not deleted).
//!
//! See the architecture doc (`docs/architektur-nfc-sam.md`, Kap. 9) for the
//! protocol rationale. Nearly every wire-level detail below is marked
//! `HARDWARE-VERIFY` because most of it has still not been exercised
//! against a real SAM AV3 + blank DESFire EV2 card — the DESFire-EV2-side
//! crypto (steps 1/3/4 above) doubly so, since this project has no access
//! to the restricted DESFire EV2/EV3 datasheet and is working from
//! commonly-published/community conventions, unlike the SAM AV3 side which
//! at least has a vendored, real-hardware-exercised reference
//! implementation to port from.
//!
//! **Known, explicitly-accepted gap in this rework**: `CommMode.Full`
//! RESPONSE verification (MAC-checking the card's reply to `ChangeKey` /
//! `WriteData`) is NOT implemented — only the native status byte is
//! checked. A response whose data was tampered with in transit but whose
//! status byte still reads `0x00` would not be caught. This is a real,
//! deliberate scope cut for this spike (see [`crypto::DesfireEv2Session::response_iv`],
//! which exists for this purpose but has no caller yet) — flagged here
//! rather than silently left out.

use super::apdu;
use super::crypto;
use super::error::SamError;
use super::flows;
use super::transport::{PiccTransport, SamTransport};

/// The factory-default AES-128 key every freshly-created DESFire app's key 0
/// ships with, given [`apdu::KEY_SETTINGS2_AES`] at `CreateApplication` time
/// — see the module-level "Incident 2026-07-15" note. Step 4 authenticates
/// directly against this KNOWN key in software; no SAM key entry is needed
/// for it at all (contrast with the old design's `FobKeys::default_key_entry_no`,
/// removed in the 2026-07-15 rework).
const FACTORY_DEFAULT_APP_KEY: [u8; 16] = [0u8; 16];

/// Fill a buffer with cryptographically secure random bytes via the OS
/// CSPRNG (`getrandom(2)` on Linux). Mirrors `ceremony::csprng_fill` — kept
/// as its own tiny copy here rather than a cross-module dependency, since
/// the two callers (key-ceremony vs. fob personalization) are otherwise
/// unrelated and this keeps each module's hardware-facing CLI independently
/// buildable.
fn csprng_fill(buf: &mut [u8]) -> Result<(), SamError> {
    getrandom::getrandom(buf).map_err(|e| SamError::Crypto(format!("CSPRNG failure: {e}")))
}

/// Generate a fresh random 16-byte token-id for one fob.
///
/// This is the value written into the fob's `StdDataFile` (bead 4ty step 6)
/// and later reported to `POST /nfc/register-to-pool`. It is **not** the
/// card's UID (which is readable in the clear by any reader and must not be
/// treated as a credential) — see the architecture doc's rationale for
/// treating the UID as clonable and the token-id (protected behind the
/// diversified app key + `CommMode.Full`) as the actual secret.
pub fn generate_token_id() -> Result<[u8; 16], SamError> {
    let mut id = [0u8; 16];
    csprng_fill(&mut id)?;
    Ok(id)
}

/// Which SAM key entries this personalization run uses.
///
/// As of the 2026-07-15 rework this is down to ONE key entry (the company
/// master key) — the old `default_key_entry_no`/`default_key_entry_ver`
/// fields (a SAM entry holding the app's factory-default AES key) are gone:
/// step 4 now authenticates against that known all-zero key entirely in
/// software, so no SAM key entry needs to hold it at all.
pub struct FobKeys {
    /// SAM key entry holding the company master key (loaded by the
    /// key-loading ceremony, bead 3pn / `ceremony::run_ceremony_on_sam`),
    /// diversified via `SAM_DumpSecretKey` in step 5 to produce the app key
    /// actually written to the card.
    ///
    /// HARDWARE-VERIFY: the SAM key entry must have
    /// [`apdu::ext_set_flags::ALLOW_DUMP_SECRET_KEY`] set in its `ExtSET`
    /// for `SAM_DumpSecretKey` to succeed — see
    /// `ceremony::COMPANY_PICC_MASTER_EXT_SET`'s doc comment (updated by
    /// this same rework) and [`flows::dump_secret_key`]'s HARDWARE-VERIFY
    /// note.
    pub company_key_entry_no: u8,
    pub company_key_entry_ver: u8,
}

/// Fixed parameters for one personalization run (constant across all fobs
/// in a batch — only the UID and the freshly generated token-id vary).
pub struct PersonalizeConfig {
    /// The application ID created on every fob.
    pub aid: [u8; 3],
    pub app_key_settings1: u8,
    pub app_key_settings2: u8,
    /// Which app key number gets diversified+changed (and later used for
    /// `CommMode.Full` access to the token-id file). `0` is this project's
    /// convention (single-key app).
    pub app_key_no: u8,
    /// The DESFire app-level key-version byte stamped on the newly-written
    /// app key (see [`crypto::build_change_key_self_cryptogram`]) — this
    /// project currently has no rotation scheme, so every fob gets the same
    /// fixed value; a config field (not a hardcoded constant) so a future
    /// key-rotation scheme has somewhere to plug in without another
    /// `personalize_fob` signature change. HARDWARE-VERIFY: `0x00` is this
    /// project's placeholder default, not derived from anything.
    pub app_key_version: u8,
    /// The `StdDataFile` file number holding the token-id.
    pub file_no: u8,
    /// `CommMode` for the token-id file's own create-time comm settings
    /// (should be [`apdu::file_comm_settings::ENCIPHERED`] — CommMode.Full
    /// end to end).
    pub file_comm_settings: u8,
    /// `AccessRights` for the token-id file (which key numbers may
    /// read/write/change it).
    pub file_access_rights: u16,
    /// Length of the token-id, in bytes. `16` for this project's
    /// 128-bit random token-id.
    pub token_id_len: u8,
    pub keys: FobKeys,
}

/// Default per-fob configuration: single-key AES app, diversified app key in
/// slot `app_key_no`, and a `StdDataFile` for the token-id.
///
/// Shared by the `sam_personalize` SSH CLI (`src/bin/sam_personalize.rs`) and
/// the kiosk binary's terminal personalize mode (admin-fob-gated batch
/// personalization, `main.rs` + `reader::pcsc_reader`) — both personalize
/// fobs the same way, so this stays a single source of truth for the
/// non-key-material parameters.
///
/// HARDWARE-VERIFY: `file_access_rights = 0x0000` (key 0 for
/// read/write/read-write/change-access-rights) is a placeholder — confirm
/// against the architecture doc / real hardware before go-live, e.g.
/// whether the token-id file should be readable by a different key number
/// than the one that writes it, or whether `key_settings1`'s
/// `ChangeKeyAccessRights` nibble (bits 4-7, not set here — defaults to
/// `0x0`, "key 0 changes all keys") matches this project's intended
/// operational model.
pub fn default_personalize_config(
    aid: [u8; 3],
    app_key_no: u8,
    file_no: u8,
    keys: FobKeys,
) -> PersonalizeConfig {
    PersonalizeConfig {
        aid,
        app_key_settings1: apdu::key_settings1::ALLOW_CHANGE_MASTER_KEY
            | apdu::key_settings1::CONFIGURATION_CHANGEABLE,
        app_key_settings2: apdu::KEY_SETTINGS2_AES | 0x01, // AES-128, 1 key.
        app_key_no,
        // Placeholder — this project has no key-rotation scheme yet; see
        // `PersonalizeConfig::app_key_version`'s doc comment.
        app_key_version: 0x00,
        file_no,
        file_comm_settings: apdu::file_comm_settings::ENCIPHERED,
        file_access_rights: 0x0000,
        token_id_len: 16,
        keys,
    }
}

/// The CSPRNG-sourced randoms one [`personalize_fob`] call needs — bundled
/// (rather than generated internally) so this function stays
/// deterministically testable against [`super::mock::MockTransport`],
/// mirroring `ceremony::HostChallenge`'s convention. Generate via
/// [`generate_personalize_challenge`] for real (CSPRNG-backed) runs.
pub struct PersonalizeChallenge {
    /// `SAM_AuthenticateHost` pass-2/pass-3 randoms (step 3.5).
    pub host_rnd1: [u8; 12],
    pub host_rnd_a: [u8; 16],
    /// The host's own `RndA` for the software `AuthenticateEV2First`
    /// handshake against the card (step 4) — INDEPENDENT of `host_rnd_a`
    /// (never reuse a challenge across two different authenticated
    /// sessions, even though both ultimately authenticate the same SAM/card
    /// pair for the same fob).
    pub ev2_rnd_a: [u8; 16],
    /// The host's `RndA` for the SECOND software `AuthenticateEV2First` — the
    /// re-authentication after the self-`ChangeKey` (step 5.5). Changing the
    /// app master key you authenticated with invalidates the DESFire session,
    /// so steps 6a/6b must run under a fresh session established with the NEW
    /// diversified key. A distinct random from `ev2_rnd_a` (never reuse a
    /// challenge across two sessions).
    pub ev2_rnd_a2: [u8; 16],
}

/// Generate a fresh [`PersonalizeChallenge`] from the OS CSPRNG — the normal
/// way to obtain one for a real (non-test) `personalize_fob` call, e.g. in
/// `src/bin/sam_personalize.rs`.
pub fn generate_personalize_challenge() -> Result<PersonalizeChallenge, SamError> {
    let mut host_rnd1 = [0u8; 12];
    csprng_fill(&mut host_rnd1)?;
    let mut host_rnd_a = [0u8; 16];
    csprng_fill(&mut host_rnd_a)?;
    let mut ev2_rnd_a = [0u8; 16];
    csprng_fill(&mut ev2_rnd_a)?;
    let mut ev2_rnd_a2 = [0u8; 16];
    csprng_fill(&mut ev2_rnd_a2)?;
    Ok(PersonalizeChallenge {
        host_rnd1,
        host_rnd_a,
        ev2_rnd_a,
        ev2_rnd_a2,
    })
}

/// Result of successfully personalizing one fob.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PersonalizedFob {
    /// The freshly written token-id, hex-encoded — this is what gets
    /// reported to `POST /nfc/register-to-pool`.
    pub token_id_hex: String,
    /// The fob's UID, hex-encoded — retained for operator logging/audit
    /// only; never sent to the backend and never treated as a credential.
    pub uid_hex: String,
}

/// Personalize one blank DESFire EV2 fob: create the application (plain,
/// unauthenticated — a factory card allows free application creation),
/// authenticate to that app's own freshly-defaulted key 0 IN SOFTWARE,
/// fetch the diversified company key from the SAM and write it as the app
/// key (also in software), create the token-id file, and write `token_id`
/// into it under `CommMode.Full`.
///
/// Steps (rewritten 2026-07-15 — see the module-level "Rework 2026-07-15"
/// note for why `SAM_AuthenticatePICC`/`SAM_ChangeKeyPICC` were abandoned;
/// see the architecture doc Kap. 9 for the broader protocol rationale):
///
/// 1. `SelectApplication(000000)` — select the PICC/master level. Plain,
///    **unauthenticated**, sent as a raw native DESFire frame straight to
///    the card ([`PiccTransport::transceive`]).
/// 2. `CreateApplication(AID, KeySettings1, KeySettings2)` — also plain and
///    unauthenticated (confirmed live: factory PICC master key settings
///    allow free application creation with zero prior auth).
///    `cfg.app_key_settings2` **must** select [`apdu::KEY_SETTINGS2_AES`]
///    so the created app's keys — including key 0, used in step 4 below —
///    default to all-zero AES.
/// 3. `SelectApplication(AID)` — select the newly created app. Still plain
///    and unauthenticated.
/// 3.5. `SAM_AuthenticateHost` — establish a SAM host session
///    ([`flows::authenticate_host`]). Needed by step 5's
///    `SAM_DumpSecretKey`; host key = the SAM host master key (entry 0,
///    factory/un-hardened = all-zero AES; parameterize after bead 2mt's
///    master-key hardening).
/// 4. **Software** `AuthenticateEV2First` against app key 0
///    ([`flows::authenticate_picc_ev2_software`]) — the host, not the SAM,
///    does every byte of this handshake, against the KNOWN all-zero AES
///    default key ([`FACTORY_DEFAULT_APP_KEY`]). Establishes the
///    [`crypto::DesfireEv2Session`] steps 5-6 below use.
/// 5. `SAM_DumpSecretKey` ([`flows::dump_secret_key`]) with `DivInput = M =
///    UID[7] || KeyNo || AID[3]` (see [`crypto::diversification_input`] —
///    identical construction to the authentication side, per the
///    architecture doc) — hands the raw diversified company key to the
///    host. The host then builds the `ChangeKey` cryptogram
///    ([`crypto::build_change_key_self_cryptogram`]), encrypts+MACs it
///    under the step-4 session ([`crypto::DesfireEv2Session::encrypt_full`]/
///    [`crypto::DesfireEv2Session::mac_command`]), and sends the native
///    `ChangeKey` command directly to the card.
/// 6. `CreateStdDataFile` (Plain — sent unwrapped, directly to the card) +
///    `WriteData` (`CommMode.Full` — encrypted+MACed under the step-4
///    session, directly to the card) of `token_id`.
///
/// ## Why steps 1-3 and 6 go straight to the card (no `send_via_sm`)
///
/// [`flows::send_via_sm`] wraps a command via `SAM_Apply_SM` and unwraps the
/// card's response via `SAM_Remove_SM` — both operate on the SAM's
/// *currently authenticated PICC session*. This design NEVER establishes
/// such a session at all (that was the old, abandoned `SAM_AuthenticatePICC`
/// design) — steps 1-3 run before any card authentication exists, and steps
/// 4-6 authenticate/protect entirely via the SOFTWARE
/// [`crypto::DesfireEv2Session`], which the SAM knows nothing about. So
/// every card-facing step in this function goes straight to
/// [`PiccTransport::transceive`]; only step 3.5's `SAM_AuthenticateHost` and
/// step 5's `SAM_DumpSecretKey` ever talk to the SAM at all.
///
/// Fail-closed: any [`SamError`] at any step aborts immediately — the
/// caller must not register `token_id` with the backend unless this
/// function returns `Ok`. Note that steps 1-3 having already run against
/// the real card means a failure at step 4 or later can still leave the
/// card with an application created on it (no cross-command DESFire
/// transaction spans `CreateApplication`..`WriteData` — see the module-
/// level doc comment). See also the module-level note on the
/// `CommMode.Full` response-verification gap this rework leaves open.
pub fn personalize_fob(
    sam: &mut dyn SamTransport,
    picc: &mut dyn PiccTransport,
    uid: [u8; 7],
    cfg: &PersonalizeConfig,
    token_id: [u8; 16],
    challenge: PersonalizeChallenge,
) -> Result<PersonalizedFob, SamError> {
    // Step 1: select the PICC/master level (AID 000000). Plain, no SAM
    // session exists yet — sent straight to the card.
    let select_picc = apdu::build_picc_select_application(&[0x00, 0x00, 0x00]);
    let resp = picc.transceive(&select_picc)?;
    apdu::parse_picc_native_status(&resp, apdu::PICC_OK)?;

    // Step 2: create the application. Also plain/unauthenticated — see the
    // doc comment above for why this is safe on a factory card, and why
    // cfg.app_key_settings2 must select KEY_SETTINGS2_AES.
    let create_app =
        apdu::build_picc_create_application(&cfg.aid, cfg.app_key_settings1, cfg.app_key_settings2);
    let resp = picc.transceive(&create_app)?;
    apdu::parse_picc_native_status(&resp, apdu::PICC_OK)?;

    // Step 3: select the newly created app so it becomes the context for
    // step 4's authentication and every step after it.
    let select_app = apdu::build_picc_select_application(&cfg.aid);
    let resp = picc.transceive(&select_app)?;
    apdu::parse_picc_native_status(&resp, apdu::PICC_OK)?;

    // Step 3.5: establish a SAM host session (Ke/Km) — needed by step 5's
    // SAM_DumpSecretKey (SAM key-entry operations require an authenticated
    // host session, or the SAM returns 6985 "conditions not satisfied";
    // proven for SAM_AuthenticatePICC on HW 2026-07-15, bead eyc, and
    // assumed to apply equally to SAM_DumpSecretKey). Host key = the SAM
    // host master key (entry 0). On a factory/un-hardened SAM that is the
    // all-zero AES key; after master-key hardening (bead 2mt) this must be
    // replaced with the hardened key (parameterize then).
    let host_session = flows::authenticate_host(
        sam,
        &[0u8; 16],
        0x00,
        0x00,
        apdu::HOST_MODE_FULL,
        challenge.host_rnd1,
        challenge.host_rnd_a,
    )?;

    // Step 4: authenticate to the app's own key 0 (all-zero AES by
    // construction, thanks to step 2's KEY_SETTINGS2_AES) ENTIRELY IN
    // SOFTWARE — no SAM involvement. Establishes the DESFire EV2 session
    // steps 5-6 use directly against the card.
    let ev2_auth = flows::authenticate_picc_ev2_software(
        picc,
        cfg.app_key_no,
        &FACTORY_DEFAULT_APP_KEY,
        challenge.ev2_rnd_a,
    )?;
    let session = ev2_auth.session; // dead after the step-5 self-ChangeKey

    // Step 5: fetch the diversified company key from the SAM, then build +
    // encrypt the native ChangeKey cryptogram in software and send it
    // straight to the card. DivInput = UID || KeyNo || AID (identical to
    // the authentication-time construction the daily punch flow uses, per
    // the architecture doc).
    let div_input = crypto::diversification_input(&uid, cfg.app_key_no, &cfg.aid);
    let diversified_key = flows::dump_secret_key(
        sam,
        &host_session,
        cfg.keys.company_key_entry_no,
        cfg.keys.company_key_entry_ver,
        &div_input,
    )?;

    let change_key_header = apdu::build_picc_change_key_header(cfg.app_key_no); // [0xC4, KeyNo]
    // EV2 self-change cryptogram: NewKey || KeyVer (NO in-cryptogram CRC — the
    // outer CMACt provides integrity), ISO 0x80-padded to 32 by encrypt_full,
    // then CBC-encrypted under the EV2 command IV.
    let cryptogram = crypto::build_change_key_self_cryptogram(&diversified_key, cfg.app_key_version);
    let enc_cryptogram = session.encrypt_full(&cryptogram)?;
    let mac = session.mac_command(&change_key_header, &enc_cryptogram);
    let mut change_key_wire = change_key_header.to_vec();
    change_key_wire.extend_from_slice(&enc_cryptogram);
    change_key_wire.extend_from_slice(&mac);

    let resp = picc.transceive(&change_key_wire)?;
    apdu::parse_picc_native_status(&resp, apdu::PICC_OK)?;
    // NOTE: no session.advance() — the self-ChangeKey of the app master key
    // just INVALIDATED this DESFire session (you are no longer authenticated
    // with the key you changed). The old `session` is now dead.

    // Step 5.5: re-authenticate to app key 0 with the NEW diversified key,
    // ENTIRELY IN SOFTWARE (no SAM — we already hold `diversified_key` in host
    // memory from step 5's SAM_DumpSecretKey). This establishes the fresh EV2
    // session steps 6a/6b run under, AND validates the ChangeKey took: if the
    // card's key isn't `diversified_key`, this AuthenticateEV2First fails.
    let ev2_reauth = flows::authenticate_picc_ev2_software(
        picc,
        cfg.app_key_no,
        &diversified_key,
        challenge.ev2_rnd_a2,
    )?;
    let mut session = ev2_reauth.session;

    // Step 6a: create the token-id file. Under an ACTIVE EV2 session this is
    // CommMode.MAC — every command (even one whose data is plain) must carry
    // the EV2 command CMACt, or the card rejects the frame as short/malformed
    // (0x7E LENGTH_ERROR, HW-observed). Wire = Cmd || cmdData || CMACt(8);
    // CMAC over Cmd || CmdCtr[LE] || TI || cmdData (no enc_data). Byte-for-byte
    // per the EV2/EV3 reference (`DesfireEv3.java` `getMacInput`: command ||
    // commandCounterLsb1 || TransactionIdentifier || options).
    let create_file = apdu::build_picc_create_std_data_file(
        cfg.file_no,
        cfg.file_comm_settings,
        cfg.file_access_rights,
        u32::from(cfg.token_id_len),
    );
    let create_mac = session.mac_command(&create_file, &[]);
    let mut create_wire = create_file;
    create_wire.extend_from_slice(&create_mac);

    let resp = picc.transceive(&create_wire)?;
    apdu::parse_picc_native_status(&resp, apdu::PICC_OK)?;
    session.advance();

    // Step 6b: write the token-id, CommMode.Full, encrypted+MACed under the
    // step-5.5 re-auth session and sent directly to the card. This is the
    // first CmdCtr>=1 command in the session — the CmdCtr encoding
    // (little-endian, confirmed against `DesfireEv3.java`'s
    // `commandCounterLsb1`/`intTo2ByteArrayInversed`) finally matters here.
    let write_header =
        apdu::build_picc_write_data_header(cfg.file_no, 0, u32::from(cfg.token_id_len));
    let enc_token_id = session.encrypt_full(&token_id)?;
    let mac = session.mac_command(&write_header, &enc_token_id);
    let mut write_wire = write_header;
    write_wire.extend_from_slice(&enc_token_id);
    write_wire.extend_from_slice(&mac);

    let resp = picc.transceive(&write_wire)?;
    apdu::parse_picc_native_status(&resp, apdu::PICC_OK)?;
    session.advance();

    Ok(PersonalizedFob {
        token_id_hex: hex::encode(token_id),
        uid_hex: hex::encode(uid),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::reader::sam::mock::MockTransport;

    fn test_config() -> PersonalizeConfig {
        PersonalizeConfig {
            aid: [0x01, 0x02, 0x03],
            app_key_settings1: apdu::key_settings1::ALLOW_CHANGE_MASTER_KEY,
            app_key_settings2: apdu::KEY_SETTINGS2_AES | 0x01,
            app_key_no: 0x00,
            app_key_version: 0x00,
            file_no: 0x00,
            file_comm_settings: apdu::file_comm_settings::ENCIPHERED,
            file_access_rights: 0xE000,
            token_id_len: 16,
            keys: FobKeys {
                company_key_entry_no: 0x02,
                company_key_entry_ver: 0x00,
            },
        }
    }

    fn test_challenge() -> PersonalizeChallenge {
        PersonalizeChallenge {
            host_rnd1: [0x01u8; 12],
            host_rnd_a: [0x02u8; 16],
            ev2_rnd_a: [0x03u8; 16],
            ev2_rnd_a2: [0x04u8; 16],
        }
    }

    /// Queue one successful plain/unauthenticated native PICC round-trip
    /// (steps 1-3: `SelectApplication(000000)`, `CreateApplication`,
    /// `SelectApplication(AID)`) — each is a single raw frame straight to
    /// the card, no SAM involved, response is just the `PICC_OK` status
    /// byte.
    fn push_plain_picc_ok(picc: &mut MockTransport) {
        picc.push_ok(vec![apdu::PICC_OK]);
    }

    /// Queue a self-consistent (proves wiring, not hardware correctness —
    /// same caveat as `flows::tests::mock_sam_for_authenticate_host`) SAM
    /// response triple for `flows::authenticate_host` (step 3.5). Returns
    /// the SAM-session `RndB` it baked into the mocked response — DISTINCT
    /// from the DESFire-card `RndB` [`push_software_ev2_ok`] uses (two
    /// independent handshakes, on independent channels, in the same
    /// personalization run) — so callers can derive the exact host-session
    /// keys `flows::authenticate_host` will end up with, for their own
    /// `SAM_DumpSecretKey` mocks.
    fn push_host_auth_ok(
        sam: &mut MockTransport,
        master_key: &[u8; 16],
        challenge: &PersonalizeChallenge,
    ) -> [u8; 16] {
        let rnd2 = [0x77u8; 12];
        let sam_rnd_b = [0x88u8; 16];

        let mut resp1 = rnd2.to_vec();
        resp1.push(0x90);
        resp1.push(0xAF);
        sam.push_ok(resp1);

        let kex = crypto::derive_kex(master_key, &challenge.host_rnd1, &rnd2).unwrap();
        let rnd_bc = crypto::aes128_cbc_encrypt_no_pad(&kex, &[0u8; 16], &sam_rnd_b).unwrap();
        let mut resp2 = vec![0u8; 8];
        resp2.extend_from_slice(&rnd_bc);
        resp2.push(0x90);
        resp2.push(0xAF);
        sam.push_ok(resp2);

        sam.push_ok(vec![0x90, 0x00]);
        sam_rnd_b
    }

    /// Queue a crypto-self-consistent software EV2-First handshake (step 4)
    /// against `FACTORY_DEFAULT_APP_KEY`, returning the `(session_enc_key,
    /// session_mac_key, ti)` the SUT should derive so the test's own
    /// `SAM_DumpSecretKey`/`ChangeKey`/`WriteData` mocks can use the SAME
    /// session state.
    fn push_software_ev2_ok(
        picc: &mut MockTransport,
        key: [u8; 16],
        rnd_a: [u8; 16],
        rnd_b: [u8; 16],
    ) -> ([u8; 16], [u8; 16], [u8; 4]) {
        let ek_rnd_b = crypto::aes128_cbc_encrypt_no_pad(&key, &[0u8; 16], &rnd_b).unwrap();
        let mut resp1 = vec![apdu::PICC_AF];
        resp1.extend_from_slice(&ek_rnd_b);
        picc.push_ok(resp1);

        // DESFire EV2 resets the IV to zero per handshake message — the
        // card's step-5 response is encrypted under IV=0 (matches the SUT).
        let ti = [0xAAu8, 0xBB, 0xCC, 0xDD];
        let rnd_a_rot = crypto::rotate_left_16(&rnd_a, 1);
        let mut plain5 = Vec::with_capacity(32);
        plain5.extend_from_slice(&ti);
        plain5.extend_from_slice(&rnd_a_rot);
        plain5.extend_from_slice(&[0u8; 12]);
        let cipher5 = crypto::aes128_cbc_encrypt_no_pad(&key, &[0u8; 16], &plain5).unwrap();
        let mut resp4 = vec![apdu::PICC_OK];
        resp4.extend_from_slice(&cipher5);
        picc.push_ok(resp4);

        let (enc_key, mac_key) = crypto::derive_desfire_ev2_session_keys(&key, &rnd_a, &rnd_b);
        (enc_key, mac_key, ti)
    }

    /// Queue a crypto-self-consistent `SAM_DumpSecretKey` Full-protection
    /// round trip (step 5) under `host_session`, returning `diversified_key`
    /// unchanged for the caller's own assertions.
    fn push_dump_secret_key_ok(
        sam: &mut MockTransport,
        host_session: &crypto::HostSessionKeys,
        diversified_key: [u8; 16],
    ) {
        // Real AV3 scheme: response at CmdCtr=1 (request 0, +1), zero IVs,
        // big-endian counter, MAC = truncate_odd(CMAC(Km, IV=0, SW || CmdCtr_BE
        // || encData)).
        let ctr_be = 1u32.to_be_bytes();
        let sw = [0x90u8, 0x00];

        let mut padded = diversified_key.to_vec();
        padded.push(0x80);
        while padded.len() % 16 != 0 {
            padded.push(0x00);
        }
        let mut my_iv = vec![0x02u8; 4];
        for _ in 1..4 {
            my_iv.extend_from_slice(&ctr_be);
        }
        let iv: [u8; 16] = crypto::aes128_cbc_encrypt_no_pad(&host_session.ke, &[0u8; 16], &my_iv)
            .unwrap()
            .try_into()
            .unwrap();
        let enc_data = crypto::aes128_cbc_encrypt_no_pad(&host_session.ke, &iv, &padded).unwrap();

        let mut mac_input = Vec::new();
        mac_input.extend_from_slice(&sw);
        mac_input.extend_from_slice(&ctr_be);
        mac_input.extend_from_slice(&enc_data);
        let mac8 = crypto::truncate_mac_odd(&crypto::cmac_aes128_with_iv(
            &host_session.km,
            &[0u8; 16],
            &mac_input,
        ));

        let mut wire = enc_data;
        wire.extend_from_slice(&mac8);
        wire.extend_from_slice(&sw);
        sam.push_ok(wire);
    }

    fn push_full_happy_path(
        sam: &mut MockTransport,
        picc: &mut MockTransport,
        challenge: &PersonalizeChallenge,
        diversified_key: [u8; 16],
    ) {
        push_plain_picc_ok(picc); // step 1: SelectApplication(000000)
        push_plain_picc_ok(picc); // step 2: CreateApplication
        push_plain_picc_ok(picc); // step 3: SelectApplication(AID)

        let sam_rnd_b = push_host_auth_ok(sam, &[0u8; 16], challenge); // step 3.5

        let card_rnd_b = [0x99u8; 16];
        // step 4: software EV2 auth with the FACTORY all-zero key
        let (_enc, _mac, _ti) =
            push_software_ev2_ok(picc, FACTORY_DEFAULT_APP_KEY, challenge.ev2_rnd_a, card_rnd_b);

        let host_session =
            crypto::derive_host_session_keys(&[0u8; 16], &challenge.host_rnd_a, &sam_rnd_b)
                .unwrap();
        push_dump_secret_key_ok(sam, &host_session, diversified_key); // step 5 (SAM half)

        picc.push_ok(vec![apdu::PICC_OK]); // step 5: native ChangeKey
        // step 5.5: re-auth (2 frames) with the NEW diversified key
        let card_rnd_b2 = [0x77u8; 16];
        let (_e2, _m2, _t2) =
            push_software_ev2_ok(picc, diversified_key, challenge.ev2_rnd_a2, card_rnd_b2);
        picc.push_ok(vec![apdu::PICC_OK]); // step 6a: CreateStdDataFile
        picc.push_ok(vec![apdu::PICC_OK]); // step 6b: WriteData
    }

    #[test]
    fn personalize_fob_happy_path_emits_expected_sequence_and_returns_ids() {
        let uid = [0x04, 0x11, 0x22, 0x33, 0x44, 0x55, 0x66];
        let token_id = [0xABu8; 16];
        let diversified_key = [0x5Au8; 16];
        let mut sam = MockTransport::new();
        let mut picc = MockTransport::new();
        let challenge = test_challenge();
        let cfg = test_config();
        push_full_happy_path(&mut sam, &mut picc, &challenge, diversified_key);

        let result =
            personalize_fob(&mut sam, &mut picc, uid, &cfg, token_id, test_challenge()).unwrap();

        assert_eq!(result.token_id_hex, hex::encode(token_id));
        assert_eq!(result.uid_hex, hex::encode(uid));

        // 3 (host auth) + 1 (dump secret key) — no other SAM traffic at all
        // in this rework (no SAM_AuthenticatePICC/SAM_ChangeKeyPICC).
        assert_eq!(sam.sent.len(), 4);
        // 1 (selectPicc) + 1 (createApp) + 1 (selectApp) + 2 (software EV2) +
        // 1 (native ChangeKey) + 2 (software EV2 re-auth) + 1 (createFile) +
        // 1 (writeData).
        assert_eq!(picc.sent.len(), 10);

        assert_eq!(
            picc.sent[0],
            apdu::build_picc_select_application(&[0x00, 0x00, 0x00])
        );
        assert_eq!(
            picc.sent[1],
            apdu::build_picc_create_application(
                &cfg.aid,
                cfg.app_key_settings1,
                cfg.app_key_settings2
            )
        );
        assert_eq!(picc.sent[2], apdu::build_picc_select_application(&cfg.aid));
        assert_eq!(picc.sent[3], apdu::build_picc_authenticate_ev2_first(0x00));
        // Step 5's native ChangeKey starts with the plain header C4 <keyNo>.
        assert_eq!(
            &picc.sent[5][0..2],
            &apdu::build_picc_change_key_header(0x00)
        );
        // Step 5.5's re-auth is a fresh AuthenticateEV2First on key 0.
        assert_eq!(picc.sent[6], apdu::build_picc_authenticate_ev2_first(0x00));
    }

    #[test]
    fn personalize_fob_diversification_input_matches_uid_keyno_aid() {
        let uid = [0x09, 0x08, 0x07, 0x06, 0x05, 0x04, 0x03];
        let token_id = [0x00u8; 16];
        let diversified_key = [0x11u8; 16];
        let mut sam = MockTransport::new();
        let mut picc = MockTransport::new();
        let challenge = test_challenge();
        let cfg = test_config();
        let expected_div_input = crypto::diversification_input(&uid, cfg.app_key_no, &cfg.aid);
        let expected_dump_cmd = apdu::build_dump_secret_key_plain(
            cfg.keys.company_key_entry_no,
            cfg.keys.company_key_entry_ver,
            &expected_div_input,
        );
        push_full_happy_path(&mut sam, &mut picc, &challenge, diversified_key);

        personalize_fob(&mut sam, &mut picc, uid, &cfg, token_id, test_challenge()).unwrap();

        // The 4th SAM APDU (3 host-auth passes, 0-indexed -> index 3) is the
        // Full-protection-WRAPPED SAM_DumpSecretKey request — recompute the
        // same wrap independently and compare, proving the div_input made
        // it into the request unchanged.
        let host_session =
            crypto::derive_host_session_keys(&[0u8; 16], &challenge.host_rnd_a, &[0x88u8; 16])
                .unwrap();
        let mut sm = crypto::SecureMessaging::new(host_session.ke, host_session.km);
        let expected_wire = sm.create_full_protection_cmd(&expected_dump_cmd).unwrap();
        assert_eq!(sam.sent[3], expected_wire);
    }

    #[test]
    fn personalize_fob_fails_closed_on_software_ev2_auth_rejection_before_any_sam_call() {
        let uid = [0x01u8; 7];
        let token_id = [0x02u8; 16];
        let mut sam = MockTransport::new();
        let mut picc = MockTransport::new();
        let challenge = test_challenge();

        // Steps 1-3 (plain, unauthenticated) succeed.
        push_plain_picc_ok(&mut picc);
        push_plain_picc_ok(&mut picc);
        push_plain_picc_ok(&mut picc);

        // Step 3.5 (host auth) succeeds.
        push_host_auth_ok(&mut sam, &[0u8; 16], &challenge);

        // Step 4: the card rejects the software EV2-First handshake at step
        // 2 (a native DESFire error status instead of AF).
        let mut resp = vec![0x7Eu8];
        resp.extend_from_slice(&[0u8; 16]);
        picc.push_ok(resp);

        let cfg = test_config();
        let err = personalize_fob(&mut sam, &mut picc, uid, &cfg, token_id, test_challenge())
            .unwrap_err();
        assert!(matches!(err, SamError::PiccStatus(0x7E)));
        // No SAM call beyond the 3 host-auth passes — DumpSecretKey never
        // attempted after a step-4 rejection.
        assert_eq!(sam.sent.len(), 3);
        assert_eq!(
            picc.sent.len(),
            4,
            "must not proceed past the rejected step-4 native frame"
        );
    }

    #[test]
    fn personalize_fob_fails_closed_on_dump_secret_key_rejection_no_card_write() {
        let uid = [0x01u8; 7];
        let token_id = [0x02u8; 16];
        let mut sam = MockTransport::new();
        let mut picc = MockTransport::new();
        let challenge = test_challenge();

        push_plain_picc_ok(&mut picc); // select PICC
        push_plain_picc_ok(&mut picc); // create app
        push_plain_picc_ok(&mut picc); // select app
        push_host_auth_ok(&mut sam, &[0u8; 16], &challenge); // step 3.5

        let rnd_b = [0x99u8; 16];
        push_software_ev2_ok(&mut picc, FACTORY_DEFAULT_APP_KEY, challenge.ev2_rnd_a, rnd_b); // step 4

        // Step 5: SAM rejects SAM_DumpSecretKey with a bad status word.
        let mut bad = vec![0u8; 2];
        bad.push(0x6A);
        bad.push(0x82);
        sam.push_ok(bad);

        let cfg = test_config();
        let err = personalize_fob(&mut sam, &mut picc, uid, &cfg, token_id, test_challenge())
            .unwrap_err();
        assert!(matches!(err, SamError::Status(0x6A82)));

        // 3 (host auth) + 1 (rejected DumpSecretKey) = 4.
        assert_eq!(sam.sent.len(), 4);
        // No ChangeKey/CreateStdDataFile/WriteData reached the card: 3
        // (steps 1-3) + 2 (software EV2) = 5.
        assert_eq!(
            picc.sent.len(),
            5,
            "must never relay ChangeKey to the card after DumpSecretKey fails"
        );
    }

    #[test]
    fn personalize_fob_fails_closed_on_write_data_rejection_after_file_created() {
        let uid = [0x01u8; 7];
        let token_id = [0x02u8; 16];
        let diversified_key = [0x33u8; 16];
        let mut sam = MockTransport::new();
        let mut picc = MockTransport::new();
        let challenge = test_challenge();

        push_plain_picc_ok(&mut picc); // select PICC
        push_plain_picc_ok(&mut picc); // create app
        push_plain_picc_ok(&mut picc); // select app
        let sam_rnd_b = push_host_auth_ok(&mut sam, &[0u8; 16], &challenge); // step 3.5

        let card_rnd_b = [0x99u8; 16];
        push_software_ev2_ok(&mut picc, FACTORY_DEFAULT_APP_KEY, challenge.ev2_rnd_a, card_rnd_b); // step 4

        let host_session =
            crypto::derive_host_session_keys(&[0u8; 16], &challenge.host_rnd_a, &sam_rnd_b)
                .unwrap();
        let cfg = test_config();
        push_dump_secret_key_ok(&mut sam, &host_session, diversified_key); // step 5

        picc.push_ok(vec![apdu::PICC_OK]); // step 5: native ChangeKey OK
        // step 5.5: re-auth (2 frames) with the diversified key
        push_software_ev2_ok(&mut picc, diversified_key, challenge.ev2_rnd_a2, [0x77u8; 16]);
        picc.push_ok(vec![apdu::PICC_OK]); // step 6a: CreateStdDataFile OK
                                           // Step 6b: card rejects WriteData with a native error status.
        picc.push_ok(vec![0x7Eu8]);

        let err = personalize_fob(&mut sam, &mut picc, uid, &cfg, token_id, test_challenge())
            .unwrap_err();
        assert!(matches!(err, SamError::PiccStatus(0x7E)));
        // No further card contact after the rejected WriteData: 3 (steps
        // 1-3) + 2 (software EV2) + 1 (ChangeKey) + 2 (re-auth) + 1 (createFile)
        // + 1 (rejected writeData) = 10.
        assert_eq!(picc.sent.len(), 10);
    }

    #[test]
    fn generate_token_id_is_16_bytes_csprng_and_non_constant() {
        let a = generate_token_id().unwrap();
        let b = generate_token_id().unwrap();
        assert_eq!(a.len(), 16);
        assert_ne!(a, [0u8; 16], "must not be the all-zero id");
        assert_ne!(a, b, "two calls must not collide");
    }

    #[test]
    fn generate_token_id_hex_encoding_round_trips() {
        let id = generate_token_id().unwrap();
        let hex_str = hex::encode(id);
        assert_eq!(hex_str.len(), 32);
        let decoded = hex::decode(&hex_str).unwrap();
        assert_eq!(decoded, id.to_vec());
    }

    #[test]
    fn generate_token_id_batch_are_all_distinct() {
        let mut ids = std::collections::HashSet::new();
        for _ in 0..20 {
            let id = generate_token_id().unwrap();
            assert!(
                ids.insert(id),
                "collision in CSPRNG token-id generation across 20 draws"
            );
        }
        assert_eq!(ids.len(), 20);
    }

    #[test]
    fn generate_personalize_challenge_produces_independent_non_constant_randoms() {
        let a = generate_personalize_challenge().unwrap();
        let b = generate_personalize_challenge().unwrap();
        assert_ne!(a.host_rnd1, [0u8; 12]);
        assert_ne!(a.host_rnd_a, [0u8; 16]);
        assert_ne!(a.ev2_rnd_a, [0u8; 16]);
        assert_ne!(
            a.host_rnd_a, a.ev2_rnd_a,
            "host session and EV2 session randoms must never coincide"
        );
        assert_ne!(a.host_rnd_a, b.host_rnd_a, "two calls must not collide");
    }
}
