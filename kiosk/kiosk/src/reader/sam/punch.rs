//! Kiosk daily punch-time SAM+DESFire EV2 authentication (bead bi0) — the
//! decision function the real PC/SC reader (`reader::pcsc_reader`,
//! `real-reader` feature) calls on every card tap, replacing the old
//! UID-only read.
//!
//! ## Rework 2026-07-15: `SAM_AuthenticatePICC` relay abandoned for the
//! offline (`SAM_DumpSecretKey`) model
//!
//! This module originally authenticated against the card's diversified app
//! key via the SAM-mediated `SAM_AuthenticatePICC` (INS `0x0A`) EV2-First
//! relay ([`flows::authenticate_picc_ev2_diversified`]) — the SAM did the
//! DESFire crypto and the host just shuttled bytes between the SAM slot and
//! the PICC slot, then [`flows::read_token_id`] relayed the `ReadData`
//! through the same SAM-tracked PICC session via `Apply_SM`/`Remove_SM`.
//!
//! Tested against real hardware 2026-07-15 (bead eyc), this relay design
//! failed: the SAM returned `6A84` on the pass-1 relay for a real
//! diversified PICC-class key entry on our AV3-mode SAM (the same failure
//! mode `personalize::personalize_fob` hit first — see that module's own
//! "Rework 2026-07-15" doc comment for the root cause and the reference
//! material this is ported from, `liblogicalaccess`). The correct model —
//! confirmed end-to-end on real hardware for personalization — is OFFLINE:
//! the SAM diversifies a key and hands the RAW key material to the host via
//! `SAM_DumpSecretKey` (INS `0xD6`), and the HOST does every byte of the
//! DESFire crypto in software from there. This module now mirrors
//! `personalize_fob`'s read-side counterpart:
//!
//! 1. `SelectApplication(aid)` — plain, unauthenticated, straight to the
//!    card (unchanged from the old design).
//! 2. `SAM_AuthenticateHost` ([`flows::authenticate_host`]) — establishes a
//!    SAM host session, needed by step 3's `SAM_DumpSecretKey`. Host key =
//!    the SAM host master key (entry 0) — see [`HOST_MASTER_KEY`]'s doc
//!    comment for the same un-hardened-SAM caveat `personalize_fob` carries.
//! 3. `SAM_DumpSecretKey` ([`flows::dump_secret_key`]) with `DivInput =
//!    UID[7] || KeyNo || AID[3]` — fetches the diversified per-fob app key
//!    from the SAM, identical construction to the personalization side.
//! 4. `AuthenticateEV2First` **entirely in software**
//!    ([`flows::authenticate_picc_ev2_software`]) against the diversified
//!    key just fetched — no SAM relay for this step at all. A card that does
//!    not hold the correctly-diversified key (a foreign card, a still-blank
//!    card, a cloned UID without the real key) fails this step and the whole
//!    call returns `Err`.
//! 5. `ReadData` under the software EV2 session, `CommMode.Full`
//!    ([`flows::read_token_id_software`]) — the card-side counterpart of
//!    `personalize_fob`'s `WriteData`, decrypting+verifying the response
//!    directly (see that function's doc comment: unlike `personalize_fob`'s
//!    explicitly-accepted response-verification gap, this read path DOES
//!    verify the response CMAC, since it must actually trust what it
//!    returns).
//!
//! `Apply_SM`/`Remove_SM` ([`flows::send_via_sm`]) can no longer be used
//! from step 1 onward, for the same reason `personalize_fob` stopped using
//! them: this design never establishes a SAM-tracked PICC session at all.
//!
//! **Fail-closed, always** (bead cqo): [`authenticate_and_read_token_id`]
//! returns `Err` on ANY unexpected status, wrong length, MAC mismatch, or
//! transport failure at ANY step — a missing/foreign/blank/cloned card, a
//! SAM rejection, a short/tampered read. There is no fallback path. The
//! caller (`pcsc_reader.rs`) must translate `Err` into "emit no
//! `CardEvent::Detected` at all" and must NEVER substitute the card's UID as
//! the badge id — the UID is used here **only** as the SAM diversification
//! input (see the architecture doc §1/§3b: a bare UID is a copyable
//! "barcode", not proof of authenticity).
//!
//! Pure/testable: this module only depends on
//! [`super::transport::SamTransport`]/[`super::transport::PiccTransport`], so
//! it is exercised here against [`super::mock::MockTransport`] with no PC/SC
//! daemon or hardware needed.

use super::apdu;
use super::crypto;
use super::error::SamError;
use super::flows;
use super::transport::{PiccTransport, SamTransport};

/// The SAM host master key (entry 0) used to establish the
/// `SAM_AuthenticateHost` session step 2 needs. Mirrors
/// `personalize::FACTORY_DEFAULT_APP_KEY`'s sibling constant in spirit:
/// on a factory/un-hardened SAM, entry 0 is the all-zero AES key; after
/// master-key hardening (bead 2mt) this must be replaced with the hardened
/// key. Hardcoded here (not a `PunchAuthConfig` field) for the same reason
/// `personalize_fob` hardcodes it inline rather than parameterizing —
/// there is exactly one host key entry in this project today, and adding a
/// config field for a value that has never varied would be premature.
const HOST_MASTER_KEY: [u8; 16] = [0u8; 16];

/// Fixed per-tenant/terminal configuration for daily punch authentication —
/// the "read" counterpart to `personalize::PersonalizeConfig` ("write").
/// Every field here must match the values used at personalization time
/// (bead 4ty) for a given fob's token-id read to succeed — see the
/// architecture doc Kap. 9 and `personalize::PersonalizeConfig`'s
/// corresponding fields.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PunchAuthConfig {
    /// The application ID selected on the card (same AID 4ty created).
    pub aid: [u8; 3],
    /// Which app key number holds the diversified key (same key number that
    /// protects the token-id file's `CommMode.Full` access). 4ty's
    /// convention is a single-key app, `app_key_no = 0`.
    pub app_key_no: u8,
    /// SAM key entry holding the company master key that gets diversified
    /// for this authentication (loaded by the key-loading ceremony, bead
    /// 3pn).
    pub sam_key_entry_no: u8,
    pub sam_key_ver: u8,
    /// The `StdDataFile` file number holding the token-id (same as 4ty's
    /// `PersonalizeConfig::file_no`).
    pub file_no: u8,
    /// Length of the token-id, in bytes. `16` for this project's 128-bit
    /// random token-id (same as 4ty's `PersonalizeConfig::token_id_len`).
    pub token_id_len: u8,
}

/// The CSPRNG-sourced randoms one [`authenticate_and_read_token_id`] call
/// needs — bundled (rather than generated internally) so this function stays
/// deterministically testable against [`super::mock::MockTransport`],
/// mirroring `personalize::PersonalizeChallenge`'s convention. Generate via
/// [`generate_punch_challenge`] for real (CSPRNG-backed) runs.
pub struct PunchChallenge {
    /// `SAM_AuthenticateHost` pass-2/pass-3 randoms (step 2).
    pub host_rnd1: [u8; 12],
    pub host_rnd_a: [u8; 16],
    /// The host's own `RndA` for the software `AuthenticateEV2First`
    /// handshake against the card (step 4) — INDEPENDENT of `host_rnd_a`
    /// (never reuse a challenge across two different authenticated
    /// sessions, even though both ultimately authenticate the same
    /// SAM/card pair for the same tap).
    pub ev2_rnd_a: [u8; 16],
}

/// Fill a buffer with cryptographically secure random bytes via the OS
/// CSPRNG (`getrandom(2)` on Linux). A tiny copy of
/// `personalize::csprng_fill` — kept separate rather than shared across
/// modules for the same reason that one is: the two callers (fob
/// personalization vs. daily punch) are otherwise unrelated and this keeps
/// each module's hardware-facing caller independently buildable.
fn csprng_fill(buf: &mut [u8]) -> Result<(), SamError> {
    getrandom::getrandom(buf).map_err(|e| SamError::Crypto(format!("CSPRNG failure: {e}")))
}

/// Generate a fresh [`PunchChallenge`] from the OS CSPRNG — the normal way
/// to obtain one for a real (non-test) `authenticate_and_read_token_id`
/// call, e.g. in `reader::pcsc_reader::authenticate_tap`.
pub fn generate_punch_challenge() -> Result<PunchChallenge, SamError> {
    let mut host_rnd1 = [0u8; 12];
    csprng_fill(&mut host_rnd1)?;
    let mut host_rnd_a = [0u8; 16];
    csprng_fill(&mut host_rnd_a)?;
    let mut ev2_rnd_a = [0u8; 16];
    csprng_fill(&mut ev2_rnd_a)?;
    Ok(PunchChallenge {
        host_rnd1,
        host_rnd_a,
        ev2_rnd_a,
    })
}

/// Authenticate a presented DESFire fob against its diversified per-fob app
/// key — fetched OFFLINE from the SAM via `SAM_DumpSecretKey`, then used
/// entirely in software — and read its stable token-id.
///
/// Steps (see the module-level "Rework 2026-07-15" doc comment for why this
/// no longer relays through the SAM, and `personalize::personalize_fob`'s
/// matching write-side steps):
///
/// 1. **`SelectApplication(aid)`** — sent directly to the card
///    (`picc.transceive`, no SAM/SM relay, no authentication needed).
/// 2. **`SAM_AuthenticateHost`** — establishes the SAM host session step 3
///    needs. Uses [`HOST_MASTER_KEY`] (entry 0) exactly like
///    `personalize_fob` does.
/// 3. Diversify the expected app key's `DivInput = UID[7] || KeyNo || AID[3]`
///    (see [`crypto::diversification_input`]) and fetch it from the SAM via
///    [`flows::dump_secret_key`].
/// 4. **Software** `AuthenticateEV2First(cfg.app_key_no)` against the
///    diversified key just fetched ([`flows::authenticate_picc_ev2_software`])
///    — no SAM involvement. A card that does not hold the
///    correctly-diversified key (a foreign card, a still-blank card, a
///    cloned UID without the real key) fails this step and the whole call
///    returns `Err`.
/// 5. Read the token-id file (`CommMode.Full`) under that software session
///    via [`flows::read_token_id_software`].
///
/// The UID parameter is used **only** to compute `DivInput` in step 3 — it
/// is never returned, logged, or otherwise treated as an identity by this
/// function. The returned `String` is always the hex-encoded **token-id**
/// read in step 5, never the UID.
///
/// Fail-closed: any [`SamError`] at any step aborts immediately via `?` and
/// no token-id is returned — the caller must not treat a partial success
/// (e.g. "auth OK but read failed") as an authenticated identity.
pub fn authenticate_and_read_token_id(
    sam: &mut dyn SamTransport,
    picc: &mut dyn PiccTransport,
    uid: [u8; 7],
    cfg: &PunchAuthConfig,
    challenge: PunchChallenge,
) -> Result<String, SamError> {
    // Step 1: select the application. Unauthenticated, no SM — must happen
    // before step 4's authentication addresses a key relative to whichever
    // application is currently selected.
    let select = apdu::build_picc_select_application(&cfg.aid);
    let select_resp = picc.transceive(&select)?;
    apdu::parse_picc_native_status(&select_resp, apdu::PICC_OK)?;

    // Step 2: establish a SAM host session — needed by step 3's
    // SAM_DumpSecretKey (SAM key-entry operations require an authenticated
    // host session, or the SAM returns 6985 "conditions not satisfied").
    let host_session = flows::authenticate_host(
        sam,
        &HOST_MASTER_KEY,
        0x00,
        0x00,
        apdu::HOST_MODE_FULL,
        challenge.host_rnd1,
        challenge.host_rnd_a,
    )?;

    // Step 3: fetch the diversified app key from the SAM. DivInput = UID ||
    // KeyNo || AID (identical construction to personalization time).
    let div_input = crypto::diversification_input(&uid, cfg.app_key_no, &cfg.aid);
    let diversified_key = flows::dump_secret_key(
        sam,
        &host_session,
        cfg.sam_key_entry_no,
        cfg.sam_key_ver,
        &div_input,
    )?;

    // Step 4: authenticate to the card's app key ENTIRELY IN SOFTWARE — no
    // SAM involvement. A card without the correctly-diversified key fails
    // here (foreign/blank/cloned card).
    let ev2_auth = flows::authenticate_picc_ev2_software(
        picc,
        cfg.app_key_no,
        &diversified_key,
        challenge.ev2_rnd_a,
    )?;
    let mut session = ev2_auth.session;

    // Step 5: read the token-id under CommMode.Full, directly against the
    // card under the software session. This is the only value this
    // function ever returns as an identity.
    let token_id =
        flows::read_token_id_software(picc, &mut session, cfg.file_no, cfg.token_id_len)?;
    Ok(hex::encode(token_id))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::reader::sam::mock::MockTransport;

    fn test_cfg() -> PunchAuthConfig {
        PunchAuthConfig {
            aid: [0x01, 0x02, 0x03],
            app_key_no: 0x00,
            sam_key_entry_no: 0x02,
            sam_key_ver: 0x00,
            file_no: 0x00,
            token_id_len: 16,
        }
    }

    fn test_challenge() -> PunchChallenge {
        PunchChallenge {
            host_rnd1: [0x01u8; 12],
            host_rnd_a: [0x02u8; 16],
            ev2_rnd_a: [0x03u8; 16],
        }
    }

    /// Queue a successful raw (non-SM) `SelectApplication` round-trip.
    fn push_select_application_ok(picc: &mut MockTransport) {
        picc.push_ok(vec![apdu::PICC_OK]);
    }

    /// Queue a self-consistent (proves wiring, not hardware correctness —
    /// same caveat as `personalize::tests::push_host_auth_ok`) SAM response
    /// triple for `flows::authenticate_host` (step 2). Returns the
    /// SAM-session `RndB` it baked into the mocked response, so callers can
    /// derive the exact host-session keys `flows::authenticate_host` will
    /// end up with, for their own `SAM_DumpSecretKey` mocks.
    fn push_host_auth_ok(sam: &mut MockTransport, challenge: &PunchChallenge) -> [u8; 16] {
        let rnd2 = [0x77u8; 12];
        let sam_rnd_b = [0x88u8; 16];

        let mut resp1 = rnd2.to_vec();
        resp1.push(0x90);
        resp1.push(0xAF);
        sam.push_ok(resp1);

        let kex = crypto::derive_kex(&HOST_MASTER_KEY, &challenge.host_rnd1, &rnd2).unwrap();
        let rnd_bc = crypto::aes128_cbc_encrypt_no_pad(&kex, &[0u8; 16], &sam_rnd_b).unwrap();
        let mut resp2 = vec![0u8; 8];
        resp2.extend_from_slice(&rnd_bc);
        resp2.push(0x90);
        resp2.push(0xAF);
        sam.push_ok(resp2);

        sam.push_ok(vec![0x90, 0x00]);
        sam_rnd_b
    }

    /// Queue a crypto-self-consistent `SAM_DumpSecretKey` Full-protection
    /// round trip (step 3) under the host session `push_host_auth_ok`
    /// established, returning `diversified_key` unchanged for the caller's
    /// own assertions. Mirrors `personalize::tests::push_dump_secret_key_ok`.
    fn push_dump_secret_key_ok(
        sam: &mut MockTransport,
        host_session: &crypto::HostSessionKeys,
        diversified_key: [u8; 16],
    ) {
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

    /// Queue a crypto-self-consistent software EV2-First handshake (step 4)
    /// against `key`, returning the `(session_enc_key, session_mac_key, ti)`
    /// the SUT should derive so the test's own `ReadData` mock can use the
    /// SAME session state. Mirrors `flows::tests::push_software_ev2_ok`.
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

    /// Encrypt `plain` the way the CARD would for a `CommMode.Full`
    /// response — under [`crypto::DesfireEv2Session::response_iv`] (the
    /// PICC -> PCD IV, distinct from the PCD -> PICC `command_iv`
    /// `encrypt_full` uses) — mirrors `flows::tests::encrypt_as_response`.
    fn encrypt_as_response(session: &crypto::DesfireEv2Session, plain: &[u8]) -> Vec<u8> {
        let mut padded = plain.to_vec();
        padded.push(0x80);
        while !padded.len().is_multiple_of(16) {
            padded.push(0x00);
        }
        let iv = session.response_iv().unwrap();
        crypto::aes128_cbc_encrypt_no_pad(&session.enc_key, &iv, &padded).unwrap()
    }

    /// Queue a `ReadData` `CommMode.Full` response (step 5) that a fresh
    /// session built from `(enc_key, mac_key, ti)` (`CmdCtr = 0` right after
    /// authentication) will accept, decrypting to `plain_data`.
    ///
    /// The response itself is built at `CmdCtr = 1`, not `0` — HW-confirmed
    /// 2026-07-15 (bead 3l1): the card increments CmdCtr BETWEEN command and
    /// response, and `read_token_id_software` advances the host's session to
    /// `1` before verifying the response MAC/decrypting. Building this at
    /// `0` would make the mock diverge from a real card.
    fn push_read_data_ok(
        picc: &mut MockTransport,
        enc_key: [u8; 16],
        mac_key: [u8; 16],
        ti: [u8; 4],
        plain_data: &[u8],
    ) {
        let response_session = crypto::DesfireEv2Session {
            enc_key,
            mac_key,
            ti,
            cmd_ctr: 1,
        };
        let enc_data = encrypt_as_response(&response_session, plain_data);
        let mac = response_session.mac_response(apdu::PICC_OK, &enc_data);
        let mut resp = vec![apdu::PICC_OK];
        resp.extend_from_slice(&enc_data);
        resp.extend_from_slice(&mac);
        picc.push_ok(resp);
    }

    /// Queue the full happy-path SAM+PICC traffic for
    /// `authenticate_and_read_token_id`, returning the diversified key so
    /// callers can assert against it if needed.
    fn push_full_happy_path(
        sam: &mut MockTransport,
        picc: &mut MockTransport,
        challenge: &PunchChallenge,
        diversified_key: [u8; 16],
        token_id: [u8; 16],
    ) {
        push_select_application_ok(picc); // step 1
        let sam_rnd_b = push_host_auth_ok(sam, challenge); // step 2
        let host_session =
            crypto::derive_host_session_keys(&HOST_MASTER_KEY, &challenge.host_rnd_a, &sam_rnd_b)
                .unwrap();
        push_dump_secret_key_ok(sam, &host_session, diversified_key); // step 3

        let card_rnd_b = [0x99u8; 16];
        let (enc_key, mac_key, ti) =
            push_software_ev2_ok(picc, diversified_key, challenge.ev2_rnd_a, card_rnd_b); // step 4
        push_read_data_ok(picc, enc_key, mac_key, ti, &token_id); // step 5
    }

    #[test]
    fn authenticate_and_read_token_id_happy_path_returns_token_id_hex() {
        let uid = [0x04, 0x11, 0x22, 0x33, 0x44, 0x55, 0x66];
        let token_id = [0xABu8; 16];
        let diversified_key = [0x5Au8; 16];
        let mut sam = MockTransport::new();
        let mut picc = MockTransport::new();
        let challenge = test_challenge();
        push_full_happy_path(&mut sam, &mut picc, &challenge, diversified_key, token_id);

        let cfg = test_cfg();
        let result =
            authenticate_and_read_token_id(&mut sam, &mut picc, uid, &cfg, test_challenge())
                .unwrap();
        assert_eq!(result, hex::encode(token_id));

        // Order sanity: SelectApplication is the very first PICC frame, sent
        // raw (no SAM Apply_SM/Remove_SM wrapping before authentication).
        assert_eq!(picc.sent[0], apdu::build_picc_select_application(&cfg.aid));
        // 3 (host auth) + 1 (dump secret key) = 4 SAM APDUs; no
        // SAM_AuthenticatePICC relay traffic at all in this rework.
        assert_eq!(sam.sent.len(), 4);
        // 1 (select) + 2 (software EV2) + 1 (ReadData) = 4 PICC frames.
        assert_eq!(picc.sent.len(), 4);
    }

    #[test]
    fn authenticate_and_read_token_id_uses_uid_only_as_diversification_input() {
        // Two taps with different UIDs but the SAME token-id on the card
        // (plausible if div_input were, by some future refactor, computed
        // wrong and reused) must still both return the token-id, never a
        // value derived from the UID — this pins the "never emit a UID"
        // contract at the type level.
        for uid in [
            [0x00u8; 7],
            [0xFFu8; 7],
            [0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07],
        ] {
            let token_id = [0x5Au8; 16];
            let diversified_key = [0x11u8; 16];
            let mut sam = MockTransport::new();
            let mut picc = MockTransport::new();
            let challenge = test_challenge();
            push_full_happy_path(&mut sam, &mut picc, &challenge, diversified_key, token_id);

            let cfg = test_cfg();
            let result =
                authenticate_and_read_token_id(&mut sam, &mut picc, uid, &cfg, test_challenge())
                    .unwrap();

            assert_eq!(result, hex::encode(token_id));
            assert_ne!(
                result,
                hex::encode(uid),
                "must never return the UID as the badge identity"
            );
        }
    }

    #[test]
    fn authenticate_and_read_token_id_fails_closed_on_select_application_rejection() {
        let uid = [0x01u8; 7];
        let mut sam = MockTransport::new();
        let mut picc = MockTransport::new();

        // Card returns a native error status instead of PICC_OK (e.g. no
        // such application — not a personalized fob, or a foreign card).
        picc.push_ok(vec![0x7Eu8]);

        let cfg = test_cfg();
        let err = authenticate_and_read_token_id(&mut sam, &mut picc, uid, &cfg, test_challenge())
            .unwrap_err();
        assert!(matches!(err, SamError::PiccStatus(0x7E)));

        assert_eq!(picc.sent.len(), 1, "must not proceed to host auth");
        assert_eq!(sam.sent.len(), 0, "must never reach the SAM");
    }

    #[test]
    fn authenticate_and_read_token_id_fails_closed_on_host_auth_rejection() {
        let uid = [0x01u8; 7];
        let mut sam = MockTransport::new();
        let mut picc = MockTransport::new();

        push_select_application_ok(&mut picc); // step 1
                                               // Step 2: SAM rejects SAM_AuthenticateHost pass 1.
        let mut bad = vec![0u8; 12];
        bad.push(0x6A);
        bad.push(0x82);
        sam.push_ok(bad);

        let cfg = test_cfg();
        let err = authenticate_and_read_token_id(&mut sam, &mut picc, uid, &cfg, test_challenge())
            .unwrap_err();
        assert!(matches!(err, SamError::Status(0x6A82)));

        assert_eq!(picc.sent.len(), 1, "must stop before any card auth");
        assert_eq!(sam.sent.len(), 1, "must stop at the first SAM rejection");
    }

    #[test]
    fn authenticate_and_read_token_id_fails_closed_on_dump_secret_key_rejection() {
        let uid = [0x01u8; 7];
        let mut sam = MockTransport::new();
        let mut picc = MockTransport::new();
        let challenge = test_challenge();

        push_select_application_ok(&mut picc); // step 1
        push_host_auth_ok(&mut sam, &challenge); // step 2
                                                 // Step 3: SAM rejects SAM_DumpSecretKey (e.g. div_input doesn't
                                                 // match this card's actual key — a foreign/blank/cloned card, or a
                                                 // SAM key entry missing the ALLOW_DUMP_SECRET_KEY ExtSET flag).
        let mut bad = vec![0u8; 2];
        bad.push(0x6A);
        bad.push(0x82);
        sam.push_ok(bad);

        let cfg = test_cfg();
        let err = authenticate_and_read_token_id(&mut sam, &mut picc, uid, &cfg, test_challenge())
            .unwrap_err();
        assert!(matches!(err, SamError::Status(0x6A82)));

        // 3 (host auth) + 1 (rejected DumpSecretKey) = 4.
        assert_eq!(sam.sent.len(), 4);
        assert_eq!(
            picc.sent.len(),
            1,
            "must never proceed to card auth after DumpSecretKey fails"
        );
    }

    #[test]
    fn authenticate_and_read_token_id_fails_closed_on_card_auth_rejection_never_reads() {
        let uid = [0x01u8; 7];
        let diversified_key = [0x22u8; 16];
        let mut sam = MockTransport::new();
        let mut picc = MockTransport::new();
        let challenge = test_challenge();

        push_select_application_ok(&mut picc); // step 1
        let sam_rnd_b = push_host_auth_ok(&mut sam, &challenge); // step 2
        let host_session =
            crypto::derive_host_session_keys(&HOST_MASTER_KEY, &challenge.host_rnd_a, &sam_rnd_b)
                .unwrap();
        push_dump_secret_key_ok(&mut sam, &host_session, diversified_key); // step 3

        // Step 4: the card rejects the software EV2-First handshake at step
        // 2 of that sub-flow (a native DESFire error status instead of AF)
        // — the card does not hold `diversified_key` (foreign/blank/cloned
        // card).
        let mut resp = vec![0x7Eu8];
        resp.extend_from_slice(&[0u8; 16]);
        picc.push_ok(resp);

        let cfg = test_cfg();
        let err = authenticate_and_read_token_id(&mut sam, &mut picc, uid, &cfg, test_challenge())
            .unwrap_err();
        assert!(matches!(err, SamError::PiccStatus(0x7E)));

        // 1 (select) + 1 (rejected EV2 step) = 2 PICC frames; no ReadData
        // attempted after a rejected card authentication.
        assert_eq!(picc.sent.len(), 2, "must stop before any read attempt");
        assert_eq!(sam.sent.len(), 4, "host auth + dump secret key only");
    }

    #[test]
    fn authenticate_and_read_token_id_fails_closed_on_read_rejection() {
        let uid = [0x01u8; 7];
        let diversified_key = [0x33u8; 16];
        let mut sam = MockTransport::new();
        let mut picc = MockTransport::new();
        let challenge = test_challenge();

        push_select_application_ok(&mut picc);
        let sam_rnd_b = push_host_auth_ok(&mut sam, &challenge);
        let host_session =
            crypto::derive_host_session_keys(&HOST_MASTER_KEY, &challenge.host_rnd_a, &sam_rnd_b)
                .unwrap();
        push_dump_secret_key_ok(&mut sam, &host_session, diversified_key);
        push_software_ev2_ok(
            &mut picc,
            diversified_key,
            challenge.ev2_rnd_a,
            [0x99u8; 16],
        );
        // Step 5: card rejects ReadData with a native error status.
        picc.push_ok(vec![0x7Eu8]);

        let cfg = test_cfg();
        let err = authenticate_and_read_token_id(&mut sam, &mut picc, uid, &cfg, test_challenge())
            .unwrap_err();
        assert!(matches!(err, SamError::PiccStatus(0x7E)));
    }

    #[test]
    fn authenticate_and_read_token_id_fails_closed_on_wrong_length_read() {
        let uid = [0x01u8; 7];
        let diversified_key = [0x44u8; 16];
        let mut sam = MockTransport::new();
        let mut picc = MockTransport::new();
        let challenge = test_challenge();

        push_select_application_ok(&mut picc);
        let sam_rnd_b = push_host_auth_ok(&mut sam, &challenge);
        let host_session =
            crypto::derive_host_session_keys(&HOST_MASTER_KEY, &challenge.host_rnd_a, &sam_rnd_b)
                .unwrap();
        push_dump_secret_key_ok(&mut sam, &host_session, diversified_key);
        let card_rnd_b = [0x99u8; 16];
        let (enc_key, mac_key, ti) =
            push_software_ev2_ok(&mut picc, diversified_key, challenge.ev2_rnd_a, card_rnd_b);
        // Card/SAM hand back the wrong number of bytes for the token-id.
        push_read_data_ok(&mut picc, enc_key, mac_key, ti, &[0x11u8; 4]);

        let cfg = test_cfg();
        let err = authenticate_and_read_token_id(&mut sam, &mut picc, uid, &cfg, test_challenge())
            .unwrap_err();
        assert!(matches!(
            err,
            SamError::UnexpectedLength {
                got: 4,
                expected: 16
            }
        ));
    }

    #[test]
    fn authenticate_and_read_token_id_div_input_matches_uid_key_no_aid() {
        let uid = [0x09, 0x08, 0x07, 0x06, 0x05, 0x04, 0x03];
        let token_id = [0x00u8; 16];
        let diversified_key = [0x66u8; 16];
        let mut sam = MockTransport::new();
        let mut picc = MockTransport::new();
        let challenge = test_challenge();
        push_full_happy_path(&mut sam, &mut picc, &challenge, diversified_key, token_id);

        let cfg = test_cfg();
        authenticate_and_read_token_id(&mut sam, &mut picc, uid, &cfg, test_challenge()).unwrap();

        // The 4th SAM APDU (3 host-auth passes, 0-indexed -> index 3) is the
        // Full-protection-wrapped SAM_DumpSecretKey request — recompute the
        // same wrap independently and compare, proving div_input made it
        // into the request unchanged.
        let expected_div_input = crypto::diversification_input(&uid, cfg.app_key_no, &cfg.aid);
        let expected_dump_cmd = apdu::build_dump_secret_key_plain(
            cfg.sam_key_entry_no,
            cfg.sam_key_ver,
            &expected_div_input,
        );
        let host_session = crypto::derive_host_session_keys(
            &HOST_MASTER_KEY,
            &challenge.host_rnd_a,
            &[0x88u8; 16], // matches push_host_auth_ok's fixture sam_rnd_b
        )
        .unwrap();
        let mut sm = crypto::SecureMessaging::new(host_session.ke, host_session.km);
        let expected_wire = sm.create_full_protection_cmd(&expected_dump_cmd).unwrap();
        assert_eq!(sam.sent[3], expected_wire);
    }
}
