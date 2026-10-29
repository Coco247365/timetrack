//! Transport-agnostic SAM/PICC protocol flows built purely from the [`apdu`]
//! builders/parsers and the [`SamTransport`]/[`PiccTransport`] traits.
//!
//! This is deliberately narrow: only the `SAM_AuthenticatePICC` EV2 8-step
//! mutual-authentication ping-pong is implemented here (it's a self-
//! contained crypto handshake, not a business flow). The key-loading
//! ceremony, key personalization, and the kiosk's daily punch-time
//! authentication flow (which will call into this) are NOT wired here —
//! see beads 3pn / 4ty / bi0.
//!
//! Fail-closed: every step propagates any [`SamError`] via `?`. There is no
//! fallback path — if any step returns an unexpected status or a
//! transport error, the whole authentication is rejected.

use super::apdu;
use super::crypto::{self, HostSessionKeys};
use super::error::SamError;
use super::transport::{PiccTransport, SamTransport};

/// Run the 3-pass `SAM_AuthenticateHost` handshake and derive the host
/// session keys (Ke, Km) used to wrap `SAM_ChangeKeyEntry` and other
/// Full-protection-mode commands.
///
/// Fail-closed: any unexpected SAM status at any pass aborts immediately —
/// the caller never receives session keys unless all three passes and the
/// final `9000` status succeeded.
///
/// `rnd1` (12 bytes) and `rnd_a` (16 bytes) are the **host-generated**
/// randoms for this session — the caller must supply them from a CSPRNG
/// (see `ceremony::csprng_bytes`); this function does no randomness
/// generation itself so it stays deterministically testable against
/// [`super::mock::MockTransport`].
///
/// HARDWARE-VERIFY: the wire layout and Ke/Km derivation are aligned to the
/// open-source `dumacp/smartcard` reference (real-hardware-exercised) per
/// the doc comments on [`apdu::build_authenticate_host_pass1`] and
/// [`crypto::derive_host_session_keys`] — not yet confirmed against this
/// project's own SAM AV3 + DS3235 spec (bead 3pn).
pub fn authenticate_host(
    sam: &mut dyn SamTransport,
    master_key: &[u8; 16],
    key_no: u8,
    key_ver: u8,
    host_mode: u8,
    rnd1: [u8; 12],
    rnd_a: [u8; 16],
) -> Result<HostSessionKeys, SamError> {
    // Pass 1: Host -> SAM.
    let pass1 = apdu::build_authenticate_host_pass1(key_no, key_ver, host_mode);
    let resp1 = sam.transmit(&pass1)?;
    let rnd2 = apdu::parse_authenticate_host_pass1_response(&resp1)?;

    // Pass 2: Host -> SAM (cmac2 over rnd2||hostMode, plus a fresh rnd1).
    let cmac2 = crypto::host_auth_pass2_cmac(master_key, &rnd2, host_mode);
    let pass2 = apdu::build_authenticate_host_pass2(&cmac2, &rnd1);
    let resp2 = sam.transmit(&pass2)?;
    let rnd_bc = apdu::parse_authenticate_host_pass2_response(&resp2)?;

    // Derive the intermediate Kex and recover the SAM's full-length RndB.
    let kex = crypto::derive_kex(master_key, &rnd1, &rnd2)?;
    let rnd_b_vec = crypto::aes128_cbc_decrypt_no_pad(&kex, &[0u8; 16], &rnd_bc)?;
    let mut rnd_b = [0u8; 16];
    rnd_b.copy_from_slice(&rnd_b_vec);

    // Pass 3: Host -> SAM (EncKex(RndA || rotl(RndB, 2))).
    let rnd_b_rot = crypto::rotate_left_16(&rnd_b, 2);
    let mut rnd_d = Vec::with_capacity(32);
    rnd_d.extend_from_slice(&rnd_a);
    rnd_d.extend_from_slice(&rnd_b_rot);
    let ecipher_vec = crypto::aes128_cbc_encrypt_no_pad(&kex, &[0u8; 16], &rnd_d)?;
    let mut ecipher = [0u8; 32];
    ecipher.copy_from_slice(&ecipher_vec);

    let pass3 = apdu::build_authenticate_host_pass3(&ecipher);
    let resp3 = sam.transmit(&pass3)?;
    apdu::parse_authenticate_host_pass3_response(&resp3)?;

    // Session established: derive Ke/Km from the long-term master key and
    // the two full-length randoms (NOT Kex — see doc comment on
    // `derive_host_session_keys`).
    crypto::derive_host_session_keys(master_key, &rnd_a, &rnd_b)
}

/// Run the `SAM_LockUnlock` **Unlock** (`state = 0x00`) 3-pass mutual-auth
/// handshake.
///
/// A factory-fresh MIFARE SAM AV3 ships LOCKED: `SAM_AuthenticateHost`
/// returns `6985` until this is run once — typically with the SAM's default
/// master key (16×0x00, keyNo 0, keyVer 0). Unlocking this way is
/// REVERSIBLE, unlike `SAM_LockUnlock`/SwitchAV2Mode (`state = 0x03`), which
/// is NOT implemented here (irreversible, out of scope for this project).
///
/// `key_no`/`key_ver` identify the key entry used for the unlock (the
/// default master key entry is `0`/`0`); `master_key` is the 16-byte AES key
/// at that entry. `rnd1` (12 bytes) and `rnd_a` (16 bytes) are host-generated
/// CSPRNG randoms — supplied by the caller (not generated here) so this
/// stays deterministically testable against [`super::mock::MockTransport`],
/// mirroring [`authenticate_host`]'s convention.
///
/// Structurally this is the SAME 3-pass mutual auth as [`authenticate_host`]
/// (pass 1: `keyNo||keyVer` -> `Rnd2`; pass 2: `macHost||rnd1` ->
/// `macSam||EncKex(RndB)`; pass 3: `EncKex(RndA||rotl(RndB,2))` -> `9000`),
/// just with `INS 0x10` instead of `0xA4` and `state` standing in for
/// `hostMode` in pass 1 — the SAM's own CMAC is additionally verified
/// directly against the leading 8 bytes of pass 2's response before
/// continuing to pass 3 (see [`apdu::parse_lock_unlock_pass2_response`]'s doc
/// comment). Unlike `authenticate_host`, the resulting session keys are
/// discarded — the caller only needs the handshake to reach `9000` so the
/// SAM transitions out of its locked state; there is no follow-on Full-mode
/// command to protect here.
///
/// Fail-closed: any unexpected SAM status word at any pass, or a `macSam`
/// that doesn't match the host's own CMAC computation over the same
/// challenge, aborts with a [`SamError`] — the SAM is never treated as
/// unlocked unless the CMAC check and all three passes' status words
/// (`90AF`, `90AF`, `9000`) check out.
///
/// HARDWARE-VERIFY: pass 1 (`80 10 00 00 02 00 00 00` ->
/// `Rnd2[12] || 90 AF`), pass 2 (accepted our `macHost`, returned
/// `macSam || EncKex(RndB) || 90 AF`), and pass 3 (`EncKex(RndA||rotl(RndB,2))`
/// -> `9000`) are all EMPIRICALLY CONFIRMED against our SAM AV3 hardware
/// (`SAM_APDU_DEBUG` trace, see bead 59m). CONFIRMED on the same trace:
/// `SAM_LockUnlock` is itself a full host authentication — it establishes
/// session keys (Ke/Km) and unlocks the SAM in one handshake, so a
/// subsequent `SAM_AuthenticateHost` on the same channel fails closed with
/// `6985` ("already authenticated", per NXP AN12704: "only one host
/// authentication per channel is allowed"). Callers must therefore use the
/// session keys returned here directly for any Full-mode command (e.g.
/// `SAM_ChangeKeyEntry`) instead of calling `authenticate_host` again — see
/// [`super::ceremony::run_ceremony_on_sam`].
pub fn lock_unlock_unlock(
    sam: &mut dyn SamTransport,
    key_no: u8,
    key_ver: u8,
    master_key: &[u8; 16],
    rnd1: [u8; 12],
    rnd_a: [u8; 16],
) -> Result<HostSessionKeys, SamError> {
    let state = apdu::LOCK_UNLOCK_STATE_UNLOCK;

    // Pass 1: Host -> SAM.
    let pass1 = apdu::build_lock_unlock_pass1(state, key_no, key_ver);
    let resp1 = sam.transmit(&pass1)?;
    let rnd2 = apdu::parse_lock_unlock_pass1_response(&resp1)?;

    // Pass 2: Host -> SAM (macHost over rnd2||state||00 00 00, plus a fresh
    // rnd1). Reuses `host_auth_pass2_cmac` as-is — its `rnd2`/`host_mode`
    // parameters are exactly `rnd2`/`state` here, the construction is
    // identical (see doc comment above).
    let mac_host = crypto::host_auth_pass2_cmac(master_key, &rnd2, state);
    let pass2 = apdu::build_lock_unlock_pass2(&mac_host, &rnd1);
    let resp2 = sam.transmit(&pass2)?;
    let (mac_sam, enc_rnd_b) = apdu::parse_lock_unlock_pass2_response(&resp2)?;

    // Verify the SAM's own CMAC (over rnd1||state||00 00 00) — fail closed
    // on any mismatch rather than trusting a bare 90AF status word.
    let expected = crypto::host_auth_pass2_cmac(master_key, &rnd1, state);
    if mac_sam != expected {
        return Err(SamError::CmacMismatch(
            "SAM_LockUnlock pass 2: SAM's CMAC did not match the host's own computation — \
             refusing to treat the SAM as unlocked"
                .to_string(),
        ));
    }

    // Derive the intermediate Kex and recover the SAM's full-length RndB —
    // identical construction to `authenticate_host`'s pass 2/3 continuation
    // (see `crypto::derive_kex`'s doc comment).
    let kex = crypto::derive_kex(master_key, &rnd1, &rnd2)?;
    let rnd_b_vec = crypto::aes128_cbc_decrypt_no_pad(&kex, &[0u8; 16], &enc_rnd_b)?;
    let mut rnd_b = [0u8; 16];
    rnd_b.copy_from_slice(&rnd_b_vec);

    // Pass 3: Host -> SAM (EncKex(RndA || rotl(RndB, 2))) — completes the
    // mutual auth so the SAM transitions out of its locked state. This is a
    // full host authentication (see doc comment above), so the resulting
    // session keys are derived and returned exactly like
    // `authenticate_host` does — the caller uses them directly for any
    // subsequent Full-mode command instead of calling `authenticate_host`
    // again (a second host authentication on the same channel fails closed
    // with 6985).
    let rnd_b_rot = crypto::rotate_left_16(&rnd_b, 2);
    let mut rnd_d = Vec::with_capacity(32);
    rnd_d.extend_from_slice(&rnd_a);
    rnd_d.extend_from_slice(&rnd_b_rot);
    let ecipher_vec = crypto::aes128_cbc_encrypt_no_pad(&kex, &[0u8; 16], &rnd_d)?;
    let mut ecipher = [0u8; 32];
    ecipher.copy_from_slice(&ecipher_vec);

    let pass3 = apdu::build_lock_unlock_pass3(&ecipher);
    let resp3 = sam.transmit(&pass3)?;
    apdu::parse_lock_unlock_pass3_response(&resp3)?;

    // Session established: derive Ke/Km from the long-term (unlock) master
    // key and the two full-length randoms — identical derivation to
    // `authenticate_host`'s final step (NOT Kex — see doc comment on
    // `derive_host_session_keys`).
    //
    // HARDWARE-VERIFY: this returns only Ke/Km. The secure-messaging state a
    // follow-on Full-mode command (`SAM_ChangeKeyEntry`) needs — the
    // `last_mac_iv` / `last_session_iv` chains and the command counter — is
    // ASSUMED to be all-zero / 0 after this handshake (see
    // `crypto::SecureMessaging::new`). liblo's own `lockUnlock` never
    // establishes a Full-protection session, so this seed is the least-certain
    // part of the port: if the SAM still returns `6985`/a MAC error on
    // ChangeKeyEntry despite a clean unlock, the seed IVs are the first thing
    // to suspect — the `SAM_APDU_DEBUG` trace on `create_full_protection_cmd`
    // shows the exact enc IV / CMAC input / MAC used.
    crypto::derive_host_session_keys(master_key, &rnd_a, &rnd_b)
}

/// Run `SAM_LockUnlock` in **ACTIVATE** mode (`state = 0x03`, "Activate SAM" /
/// SwitchToAV2) — the one-time, **IRREVERSIBLE** activation of a factory (ICS)
/// SAM AV3 into MCS so `SAM_AuthenticateHost`/`SAM_ChangeKeyEntry` (GCS) stop
/// returning `6985`.
///
/// Identical 3-pass mutual-auth handshake to [`lock_unlock_unlock`], with two
/// differences: pass 1 is [`apdu::build_lock_unlock_activate_pass1`] (P1=0x03,
/// Lc=05 with the `00 00 00` MaxChainBlocks trailer), and every CMAC keys on
/// `state = 0x03`. Pass 2/3 framing is byte-identical (their P1 is always 0x00).
/// The returned session keys are the handshake's, kept only for symmetry with
/// `lock_unlock_unlock`.
pub fn lock_unlock_activate(
    sam: &mut dyn SamTransport,
    key_no: u8,
    key_ver: u8,
    master_key: &[u8; 16],
    rnd1: [u8; 12],
    rnd_a: [u8; 16],
) -> Result<HostSessionKeys, SamError> {
    let state = apdu::LOCK_UNLOCK_STATE_ACTIVATE;

    // Pass 1 (P1=0x03, MaxChainBlocks 000000).
    let pass1 = apdu::build_lock_unlock_activate_pass1(key_no, key_ver);
    let resp1 = sam.transmit(&pass1)?;
    let rnd2 = apdu::parse_lock_unlock_pass1_response(&resp1)?;

    // Pass 2: macHost over rnd2||state||000, plus a fresh rnd1.
    let mac_host = crypto::host_auth_pass2_cmac(master_key, &rnd2, state);
    let pass2 = apdu::build_lock_unlock_pass2(&mac_host, &rnd1);
    let resp2 = sam.transmit(&pass2)?;
    let (mac_sam, enc_rnd_b) = apdu::parse_lock_unlock_pass2_response(&resp2)?;

    // Verify the SAM's own CMAC (over rnd1||state||000) — fail closed.
    let expected = crypto::host_auth_pass2_cmac(master_key, &rnd1, state);
    if mac_sam != expected {
        return Err(SamError::CmacMismatch(
            "SAM_LockUnlock/Activate pass 2: SAM CMAC mismatch — refusing to treat as activated"
                .to_string(),
        ));
    }

    // Recover RndB and complete pass 3 — identical to lock_unlock_unlock.
    let kex = crypto::derive_kex(master_key, &rnd1, &rnd2)?;
    let rnd_b_vec = crypto::aes128_cbc_decrypt_no_pad(&kex, &[0u8; 16], &enc_rnd_b)?;
    let mut rnd_b = [0u8; 16];
    rnd_b.copy_from_slice(&rnd_b_vec);

    let rnd_b_rot = crypto::rotate_left_16(&rnd_b, 2);
    let mut rnd_d = Vec::with_capacity(32);
    rnd_d.extend_from_slice(&rnd_a);
    rnd_d.extend_from_slice(&rnd_b_rot);
    let ecipher_vec = crypto::aes128_cbc_encrypt_no_pad(&kex, &[0u8; 16], &rnd_d)?;
    let mut ecipher = [0u8; 32];
    ecipher.copy_from_slice(&ecipher_vec);

    let pass3 = apdu::build_lock_unlock_pass3(&ecipher);
    let resp3 = sam.transmit(&pass3)?;
    apdu::parse_lock_unlock_pass3_response(&resp3)?;

    crypto::derive_host_session_keys(master_key, &rnd_a, &rnd_b)
}

/// Result of a successful `SAM_AuthenticatePICC` EV2-First authentication.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Ev2AuthResult {
    /// The `caps` bytes returned by the SAM alongside the final `9000`
    /// status word (PICC/SAM capability bytes agreed during EV2 auth).
    pub caps: Vec<u8>,
}

/// Run the 8-step `SAM_AuthenticatePICC` EV2-First handshake described in
/// the architecture doc:
///
/// 1. Host -> PICC: `AuthenticateEV2First`
/// 2. PICC -> Host: `AF || Ek(RndB)`
/// 3. Host -> SAM: `SAM_AuthenticatePICC` pass 1
/// 4. SAM -> Host: `PCDChalResp[32] || 90 AF`
/// 5. Host -> PICC: `AF || PCDChalResp`
/// 6. PICC -> Host: `00 || PDResp[32]`
/// 7. Host -> SAM: `SAM_AuthenticatePICC` pass 2
/// 8. SAM -> Host: `caps || 90 00`
///
/// `picc_key_no` is the DESFire on-card key number (used in step 1);
/// `sam_key_entry_no`/`sam_key_ver` address the SAM's internal key entry
/// (used in step 3) — these are different address spaces and must not be
/// conflated.
///
/// Card makes `RndB`, the SAM makes `RndA`; session keys and the SAM's R/W
/// counters stay inside the SAM — this function never sees them.
///
/// **Currently unused by any active flow** (bead 4ty rework, 2026-07-15):
/// `personalize::personalize_fob` was this function's only caller and now
/// authenticates against the app's default key entirely in software
/// ([`authenticate_picc_ev2_software`]) instead of relaying through the SAM
/// — see the `personalize` module-level "Incident 2026-07-15" doc comment.
/// Kept (not deleted) as a general-purpose SAM-relayed EV2 primitive against
/// a NON-diversified key — [`authenticate_picc_ev2_diversified`] (the
/// diversified sibling this shares its implementation with via
/// [`authenticate_picc_ev2_steps`]) is still very much in active use by
/// `punch::authenticate_and_read_token_id` (bead bi0, the daily punch flow),
/// so this file's shared plumbing stays exercised either way.
pub fn authenticate_picc_ev2(
    sam: &mut dyn SamTransport,
    picc: &mut dyn PiccTransport,
    picc_key_no: u8,
    sam_key_entry_no: u8,
    sam_key_ver: u8,
) -> Result<Ev2AuthResult, SamError> {
    authenticate_picc_ev2_steps(sam, picc, picc_key_no, |ek_rnd_b| {
        apdu::build_authenticate_picc_pass1(
            apdu::AUTH_PICC_P1_EV2_FIRST,
            sam_key_entry_no,
            sam_key_ver,
            ek_rnd_b,
        )
    })
}

/// Run the 8-step `SAM_AuthenticatePICC` EV2-First handshake against a
/// **diversified** per-fob key.
///
/// Identical to [`authenticate_picc_ev2`] except step 3 uses
/// [`apdu::build_authenticate_picc_pass1_diversified`] (a different P1 value
/// plus a trailing `div_input`) — see the HARDWARE-VERIFY note on
/// [`apdu::AUTH_PICC_P1_EV2_FIRST_DIV`] for what's unconfirmed about this.
///
/// `div_input` must be the same `DivInput` used to diversify+write the app
/// key at personalization time (`M = UID[7] || KeyNo || AID[3]`, see
/// [`crypto::diversification_input`]) — a mismatched `div_input` (wrong UID,
/// wrong key number, or wrong AID) makes the SAM derive a different key than
/// the one on the card, and the handshake fails closed at step 3/4 exactly
/// like a genuinely foreign/cloned card would.
///
/// **Currently unused by any active flow** (bead bi0 rework, 2026-07-15):
/// this was the daily punch flow's (`punch::authenticate_and_read_token_id`)
/// only caller, and it abandoned the `SAM_AuthenticatePICC` relay for the
/// same reason `personalize::personalize_fob` did (bead 4ty) — the SAM
/// returned `6A84` on this relay for a real diversified PICC-class key entry
/// on our AV3-mode SAM. The punch flow now authenticates entirely in
/// software via [`authenticate_picc_ev2_software`] against a key fetched
/// with [`dump_secret_key`], exactly like personalization. Kept (not
/// deleted), mirroring [`authenticate_picc_ev2`]'s own "superseded, not
/// removed" precedent — the shared [`authenticate_picc_ev2_steps`] plumbing
/// stays exercised by [`authenticate_picc_ev2`]'s own tests either way.
pub fn authenticate_picc_ev2_diversified(
    sam: &mut dyn SamTransport,
    picc: &mut dyn PiccTransport,
    picc_key_no: u8,
    sam_key_entry_no: u8,
    sam_key_ver: u8,
    div_input: &[u8],
) -> Result<Ev2AuthResult, SamError> {
    authenticate_picc_ev2_steps(sam, picc, picc_key_no, |ek_rnd_b| {
        apdu::build_authenticate_picc_pass1_diversified(
            sam_key_entry_no,
            sam_key_ver,
            ek_rnd_b,
            div_input,
        )
    })
}

/// Shared implementation of the 8-step `SAM_AuthenticatePICC` EV2-First
/// handshake for [`authenticate_picc_ev2`] and
/// [`authenticate_picc_ev2_diversified`] — only step 3 (building the SAM
/// pass-1 APDU from the card's `Ek(RndB)`) differs between a diversified and
/// non-diversified authentication, so it is the only step parameterized via
/// `build_step3`.
fn authenticate_picc_ev2_steps(
    sam: &mut dyn SamTransport,
    picc: &mut dyn PiccTransport,
    picc_key_no: u8,
    build_step3: impl FnOnce(&[u8; 16]) -> Vec<u8>,
) -> Result<Ev2AuthResult, SamError> {
    // Step 1: Host -> PICC.
    let step1 = apdu::build_picc_authenticate_ev2_first(picc_key_no);
    let resp1 = picc.transceive(&step1)?;
    // Step 2: PICC -> Host: AF || Ek(RndB) (16 bytes — one AES block).
    let ek_rnd_b = apdu::parse_picc_frame_ek_rnd_b(&resp1, apdu::PICC_AF)?;

    // Step 3: Host -> SAM.
    let step3 = build_step3(&ek_rnd_b);
    let resp3 = sam.transmit(&step3)?;
    // Step 4: SAM -> Host: PCDChalResp[32] || 90 AF.
    let pcd_chal_resp = apdu::parse_authenticate_picc_pass1_response(&resp3)?;

    // Step 5: Host -> PICC: AF || PCDChalResp.
    let step5 = apdu::build_picc_continue(&pcd_chal_resp);
    let resp5 = picc.transceive(&step5)?;
    // Step 6: PICC -> Host: 00 || PDResp[32].
    let pd_resp = apdu::parse_picc_frame_pd_resp(&resp5, apdu::PICC_OK)?;

    // Step 7: Host -> SAM.
    let step7 = apdu::build_authenticate_picc_pass2(&pd_resp);
    let resp7 = sam.transmit(&step7)?;
    // Step 8: SAM -> Host: caps || 90 00.
    let caps = apdu::parse_authenticate_picc_pass2_response(&resp7)?;

    Ok(Ev2AuthResult { caps })
}

/// Result of a successful SOFTWARE `AuthenticateEV2First` (bead 4ty rework,
/// 2026-07-15) — no SAM involved at all; the host does the DESFire crypto
/// directly against a KNOWN key (this project's only caller:
/// `personalize::personalize_fob` step 4, against a freshly-created app's
/// factory all-zero AES key 0). See [`crypto::DesfireEv2Session`] and its
/// module-level HARDWARE-VERIFY note.
#[derive(Debug)]
pub struct Ev2SoftwareAuthResult {
    pub session: crypto::DesfireEv2Session,
}

/// Run `AuthenticateEV2First` entirely in software against `key` — no SAM
/// relay, unlike [`authenticate_picc_ev2`]/[`authenticate_picc_ev2_diversified`]
/// above. `rnd_a` is the host-generated 16-byte CSPRNG challenge
/// (caller-supplied, like every other randomness in this module, so this
/// stays deterministically testable against [`super::mock::MockTransport`]).
///
/// Steps (commonly-published DESFire EV1/EV2 native `AuthenticateEV2First`
/// handshake — HARDWARE-VERIFY, see [`crypto::DesfireEv2Session`]'s
/// module-level note; NONE of this has been exercised against this
/// project's own card):
/// 1. Host -> PICC: `71 <keyNo> 00` ([`apdu::build_picc_authenticate_ev2_first`]).
/// 2. PICC -> Host: `AF || Ek,IV=0(RndB)[16]`.
/// 3. Host decrypts (AES-CBC, IV=0) -> RndB. Generates RndA. `RndB' = RndB`
///    rotated left by ONE byte (8 bits) — [`crypto::rotate_left_16`]`(_, 1)`.
/// 4. Host -> PICC: `AF || Ek,IV=0(RndA || RndB')[32]`.
/// 5. PICC -> Host: `00 || Ek,IV=<step 4's own last ciphertext block>(TI ||
///    RndA' || PDcap2 || PCDcap2)[32]` — the card's response is encrypted
///    CHAINED from step 4's own ciphertext (its last 16-byte block), NOT a
///    fresh `IV=0`. This chaining detail is the single most commonly-cited
///    DESFire EV1/EV2 auth convention across independent open-source
///    reimplementations this reading is aligned to, but is unconfirmed
///    against this project's own card.
/// 6. Host decrypts with that chained IV -> `TI[4] || RndA'[16] ||
///    PDcap2[6] || PCDcap2[6]`. Verifies `RndA' == rotate_left_16(RndA, 1)`
///    — fails closed (an impersonation/relay-attack check) on any mismatch.
/// 7. Derives `(SesAuthENCKey, SesAuthMACKey)` via
///    [`crypto::derive_desfire_ev2_session_keys`] and returns a
///    [`crypto::DesfireEv2Session`] seeded with `ti` and `cmd_ctr = 0`.
///
/// Fail-closed: any unexpected native status byte, wrong-length response, or
/// a RndA' mismatch aborts via `?`/an explicit [`SamError`] — the caller
/// never receives a session unless the full handshake (including the RndA'
/// check) succeeded.
pub fn authenticate_picc_ev2_software(
    picc: &mut dyn PiccTransport,
    key_no: u8,
    key: &[u8; 16],
    rnd_a: [u8; 16],
) -> Result<Ev2SoftwareAuthResult, SamError> {
    // Step 1/2: Host -> PICC -> Host: AF || Ek,IV=0(RndB).
    let step1 = apdu::build_picc_authenticate_ev2_first(key_no);
    let resp1 = picc.transceive(&step1)?;
    let ek_rnd_b = apdu::parse_picc_frame_ek_rnd_b(&resp1, apdu::PICC_AF)?;

    // Step 3: decrypt RndB (IV=0), compute RndB' = rotl(RndB, 1 byte).
    let rnd_b_vec = crypto::aes128_cbc_decrypt_no_pad(key, &[0u8; 16], &ek_rnd_b)?;
    let mut rnd_b = [0u8; 16];
    rnd_b.copy_from_slice(&rnd_b_vec);
    let rnd_b_rot = crypto::rotate_left_16(&rnd_b, 1);

    // Step 4: encrypt RndA || RndB' under IV=0; keep the ciphertext — its
    // last block chains into step 5's decrypt IV (see doc comment above).
    let mut plain4 = Vec::with_capacity(32);
    plain4.extend_from_slice(&rnd_a);
    plain4.extend_from_slice(&rnd_b_rot);
    let step4_cipher = crypto::aes128_cbc_encrypt_no_pad(key, &[0u8; 16], &plain4)?;
    let mut step4_block = [0u8; 32];
    step4_block.copy_from_slice(&step4_cipher);
    let step4 = apdu::build_picc_continue(&step4_block);
    let resp4 = picc.transceive(&step4)?;
    let pd_resp = apdu::parse_picc_frame_pd_resp(&resp4, apdu::PICC_OK)?;

    // Step 5/6: decrypt the card's response. DESFire EV2 AuthenticateEV2First
    // resets the AES-CBC IV to zero for EACH handshake message (unlike legacy
    // EV1 chaining) — HW-confirmed here because step 4 encrypted with IV=0 and
    // the card ACCEPTED it (returned status 00). The card therefore encrypts
    // its own response with IV=0 too; the per-message session IV mechanism
    // only begins AFTER the handshake completes. (step4_cipher is retained
    // only to build the request, not as a chaining IV.)
    let plain_resp = crypto::aes128_cbc_decrypt_no_pad(key, &[0u8; 16], &pd_resp)?;
    if plain_resp.len() != 32 {
        return Err(SamError::UnexpectedLength {
            got: plain_resp.len(),
            expected: 32,
        });
    }
    let mut ti = [0u8; 4];
    ti.copy_from_slice(&plain_resp[0..4]);
    let mut rnd_a_rot_from_card = [0u8; 16];
    rnd_a_rot_from_card.copy_from_slice(&plain_resp[4..20]);
    let expected_rnd_a_rot = crypto::rotate_left_16(&rnd_a, 1);
    if rnd_a_rot_from_card != expected_rnd_a_rot {
        return Err(SamError::CmacMismatch(
            "AuthenticateEV2First: card's RndA' does not match the host's own rotation of RndA \
             — refusing to trust this session (possible relay/impersonation, or a wrong key)"
                .to_string(),
        ));
    }

    // Step 7: derive the EV2 session keys.
    let (enc_key, mac_key) = crypto::derive_desfire_ev2_session_keys(key, &rnd_a, &rnd_b);
    Ok(Ev2SoftwareAuthResult {
        session: crypto::DesfireEv2Session::new(enc_key, mac_key, ti),
    })
}

/// Fetch the diversified company key from the SAM via `SAM_DumpSecretKey`
/// (INS `0xD6`), Full-protection wrapped/unwrapped under `host_session` —
/// bead 4ty's rework (2026-07-15) replacing the old
/// `SAM_AuthenticatePICC`-relay design (see the `personalize` module-level
/// "Incident 2026-07-15" doc comment). `entry_no`/`entry_ver` identify the
/// SAM key entry holding the company master key (loaded by the key-loading
/// ceremony, bead 3pn); `div_input` must be the same `DivInput` the app key
/// will later be authenticated against by the daily punch flow (`M =
/// UID[7] || KeyNo || AID[3]`, [`crypto::diversification_input`]).
///
/// Fail-closed: any [`SamError`] — including a `SamError::CmacMismatch` from
/// the response verification, or a wrong-length/wrong-status result — aborts
/// before any card write.
///
/// HARDWARE-VERIFY (significant, several open questions bundled here — see
/// also [`apdu::build_dump_secret_key_plain`] and
/// [`crypto::SecureMessaging::verify_and_decrypt_response`]'s own notes):
/// - Whether the SAM key entry actually needs
///   [`apdu::ext_set_flags::ALLOW_DUMP_SECRET_KEY`] set to permit this
///   command — this project's ceremony
///   (`ceremony::COMPANY_PICC_MASTER_EXT_SET`) has been updated to set it,
///   but any SAM already keyed by an OLDER ceremony run will need
///   re-keying before this works.
/// - This is the very first caller in this codebase to exercise ANY SAM
///   Full-protection RESPONSE decryption; only the request-wrapping half
///   has hardware-adjacent confidence so far (`SAM_ChangeKeyEntry`, bead
///   59m).
pub fn dump_secret_key(
    sam: &mut dyn SamTransport,
    host_session: &HostSessionKeys,
    entry_no: u8,
    entry_ver: u8,
    div_input: &[u8],
) -> Result<[u8; 16], SamError> {
    let plain_cmd = apdu::build_dump_secret_key_plain(entry_no, entry_ver, div_input);
    let mut sm = crypto::SecureMessaging::new(host_session.ke, host_session.km);
    let data = sm.full_protection_round_trip(sam, &plain_cmd)?;

    if data.len() < 2 {
        return Err(SamError::UnexpectedLength {
            got: data.len(),
            expected: 2,
        });
    }
    let (key_bytes, sw_bytes) = data.split_at(data.len() - 2);
    let sw = u16::from_be_bytes([sw_bytes[0], sw_bytes[1]]);
    if sw != 0x9000 {
        return Err(SamError::Status(sw));
    }
    if key_bytes.len() != 16 {
        return Err(SamError::UnexpectedLength {
            got: key_bytes.len(),
            expected: 16,
        });
    }
    let mut key = [0u8; 16];
    key.copy_from_slice(key_bytes);
    Ok(key)
}

/// Relay one native DESFire command to the card through the SAM's secure
/// messaging (S-mode): `Apply_SM` asks the SAM to (optionally) encrypt/MAC
/// `desfire_cmd` for the currently-authenticated PICC session and returns
/// the exact bytes to send to the card; those bytes go to the card via
/// `picc.transceive`; the card's raw response is handed back to the SAM via
/// `Remove_SM`, which verifies/decrypts it and returns the plain data.
///
/// Call this even for [`apdu::COMM_MODE_PLAIN`] commands (`CreateApplication`,
/// `SelectApplication`, ...) — per the doc comment on
/// [`apdu::build_apply_sm`], this keeps the SAM's internal R/W counters
/// synchronized with the card even when no encryption/MAC is applied.
///
/// **Currently unused by any active flow** (bead bi0 rework, 2026-07-15):
/// `punch::authenticate_and_read_token_id` was this function's last caller
/// (via [`read_token_id`] below) and, like `personalize::personalize_fob`
/// before it (bead 4ty rework), abandoned the `SAM_AuthenticatePICC`-relayed
/// PICC session for the offline model — every punch-flow command now goes
/// straight to the card ([`PiccTransport::transceive`]), protected by the
/// software [`crypto::DesfireEv2Session`] (see [`read_token_id_software`]).
/// Kept (not deleted) for the same reason as [`authenticate_picc_ev2_diversified`]
/// — a general-purpose SAM-relayed primitive against an established
/// `SAM_AuthenticatePICC` session, exercised by its own tests below.
///
/// Fail-closed: any unexpected SAM/PICC status at any of the three
/// sub-steps aborts immediately via `?`.
///
/// HARDWARE-VERIFY: this assumes `Apply_SM`'s response data (after
/// `parse_status_ok`) is exactly the bytes to send to the card as-is (no
/// further framing needed), and that `Remove_SM`'s response data is the
/// final plain/verified payload the caller wants. Neither is confirmed
/// against real SAM AV3 + DESFire hardware — see the architecture doc
/// Kap. 9 and the module-level HARDWARE-VERIFY note on `apdu.rs`'s
/// DESFire-native section.
pub fn send_via_sm(
    sam: &mut dyn SamTransport,
    picc: &mut dyn PiccTransport,
    comm_mode: u8,
    desfire_cmd: &[u8],
) -> Result<Vec<u8>, SamError> {
    let apply_apdu = apdu::build_apply_sm(comm_mode, desfire_cmd);
    let apply_resp = sam.transmit(&apply_apdu)?;
    let wrapped_cmd = apdu::parse_status_ok(&apply_resp)?;

    let picc_resp = picc.transceive(wrapped_cmd)?;

    let remove_apdu = apdu::build_remove_sm(&picc_resp);
    let remove_resp = sam.transmit(&remove_apdu)?;
    let data = apdu::parse_status_ok(&remove_resp)?;
    Ok(data.to_vec())
}

/// Read the token-id file via `Apply_SM`/`Remove_SM` in
/// [`apdu::COMM_MODE_FULL`] (CommMode.Full end to end, matching how 4ty
/// created and wrote the file — see `personalize::PersonalizeConfig`).
/// Must be called only after a successful
/// [`authenticate_picc_ev2_diversified`] (or, for testing/tooling,
/// [`authenticate_picc_ev2`]) — this function does no authentication itself.
///
/// Fail-closed: propagates any [`SamError`] from [`send_via_sm`] via `?`,
/// and additionally rejects a response whose length doesn't exactly match
/// `len` (a short/garbled/replayed response is treated the same as any
/// other unexpected-shape failure — never silently truncated or padded).
///
/// **Currently unused by any active flow** (bead bi0 rework, 2026-07-15) —
/// see [`send_via_sm`]'s doc comment. [`read_token_id_software`] is the
/// daily punch flow's replacement. Kept for the same reason as
/// [`send_via_sm`]/[`authenticate_picc_ev2_diversified`].
pub fn read_token_id(
    sam: &mut dyn SamTransport,
    picc: &mut dyn PiccTransport,
    file_no: u8,
    len: u8,
) -> Result<Vec<u8>, SamError> {
    let read_cmd = apdu::build_picc_read_data(file_no, 0, u32::from(len));
    let data = send_via_sm(sam, picc, apdu::COMM_MODE_FULL, &read_cmd)?;
    if data.len() != usize::from(len) {
        return Err(SamError::UnexpectedLength {
            got: data.len(),
            expected: usize::from(len),
        });
    }
    Ok(data)
}

/// Read the token-id file (bead bi0 rework, 2026-07-15) directly against the
/// card under an already-established software [`crypto::DesfireEv2Session`]
/// (see [`authenticate_picc_ev2_software`]) — the offline replacement for
/// [`read_token_id`], mirroring how `personalize::personalize_fob` reads/
/// writes the same file straight to the card instead of relaying through the
/// SAM's `Apply_SM`/`Remove_SM`.
///
/// `ReadData` carries no data field on the request (there is nothing for the
/// host to encrypt), so — exactly like `personalize_fob`'s `CreateStdDataFile`
/// step — the request is sent `CommMode.MAC`: the plain command header
/// followed by [`crypto::DesfireEv2Session::mac_command`]'s CMACt, no
/// `encrypt_full` call. The **response**, however, carries the enciphered
/// token-id (the file itself was created `CommMode.ENCIPHERED` by
/// personalization) — `<status> || EncData || CMACt(8)` — which this
/// function verifies via [`crypto::DesfireEv2Session::mac_response`] and
/// decrypts via [`crypto::DesfireEv2Session::decrypt_full`].
///
/// Fail-closed, same as [`read_token_id`]: any unexpected native status byte,
/// a response too short to carry an 8-byte MAC, a MAC mismatch (rejects
/// rather than trusting unverified data — this does NOT repeat
/// `personalize_fob`'s explicitly-accepted response-verification gap, since
/// this is a read path that must actually trust what it returns), or a
/// decrypted length that doesn't exactly match `len` are all hard errors.
/// Advances `session`'s command counter exactly once, immediately after the
/// card accepts the command (native status PICC_OK) — see the inline
/// comment at the call site for why this must happen BEFORE the response
/// MAC/decrypt, not after.
///
/// HARDWARE-CONFIRMED 2026-07-15 (zeit-terminal-01, bead 3l1, via an
/// in-process `PUNCH_MAC_DIAG` brute-force against a real card — see git
/// history for the retired diagnostic): this codebase's first EV2
/// `CommMode.Full` **response** consumer. The request-side `CommMode.MAC`-
/// only framing (no data to encrypt), the response wire shape (`status ||
/// EncData || CMACt(8)`, no length/offset echo), and the response-MAC/IV
/// construction ([`crypto::DesfireEv2Session::mac_response`]/`response_iv`,
/// both at `CmdCtr = N+1`) are all confirmed correct against real hardware.
pub fn read_token_id_software(
    picc: &mut dyn PiccTransport,
    session: &mut crypto::DesfireEv2Session,
    file_no: u8,
    len: u8,
) -> Result<Vec<u8>, SamError> {
    let read_cmd = apdu::build_picc_read_data(file_no, 0, u32::from(len));
    let mac = session.mac_command(&read_cmd, &[]);
    let mut wire = read_cmd;
    wire.extend_from_slice(&mac);

    let resp = picc.transceive(&wire)?;
    let rest = apdu::parse_picc_native_status(&resp, apdu::PICC_OK)?;

    // HARDWARE-CONFIRMED 2026-07-15 (zeit-terminal-01, bead 3l1): DESFire EV2
    // increments CmdCtr BETWEEN command and response. The command MAC/IV
    // above used CmdCtr=N (already sent); the RESPONSE MAC and response
    // decrypt IV (`mac_response`/`decrypt_full` via `response_iv`, below)
    // both use CmdCtr=N+1. Advance exactly once, right here — immediately
    // after the card has accepted the command (native status PICC_OK) and
    // BEFORE computing/verifying the response MAC or decrypting — so both
    // run at N+1 and the session ends at N+1 for any subsequent command.
    session.advance();

    if rest.len() < 8 {
        return Err(SamError::UnexpectedLength {
            got: rest.len(),
            expected: 8,
        });
    }
    let (enc_data, mac_bytes) = rest.split_at(rest.len() - 8);

    let expected_mac = session.mac_response(apdu::PICC_OK, enc_data);
    if mac_bytes != expected_mac {
        return Err(SamError::CmacMismatch(
            "ReadData: card's CommMode.Full response CMAC did not verify — refusing to trust \
             this token-id"
                .to_string(),
        ));
    }

    let plain = session.decrypt_full(enc_data)?;
    if plain.len() != usize::from(len) {
        return Err(SamError::UnexpectedLength {
            got: plain.len(),
            expected: usize::from(len),
        });
    }
    Ok(plain)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::reader::sam::mock::MockTransport;

    #[test]
    fn eight_step_handshake_success_emits_apdus_in_order_and_parses_caps() {
        let ek_rnd_b = [0x11u8; 16];
        let pcd_chal_resp = [0x22u8; 32];
        let pd_resp = [0x33u8; 32];
        let caps = vec![0xCA, 0xFE];

        let mut picc = MockTransport::new();
        // Step 2 response: AF || Ek(RndB) (16 bytes)
        let mut resp1 = vec![apdu::PICC_AF];
        resp1.extend_from_slice(&ek_rnd_b);
        picc.push_ok(resp1);
        // Step 6 response: 00 || PDResp
        let mut resp5 = vec![apdu::PICC_OK];
        resp5.extend_from_slice(&pd_resp);
        picc.push_ok(resp5);

        let mut sam = MockTransport::new();
        // Step 4 response: PCDChalResp || 90 AF
        let mut resp3 = pcd_chal_resp.to_vec();
        resp3.push(0x90);
        resp3.push(0xAF);
        sam.push_ok(resp3);
        // Step 8 response: caps || 90 00
        let mut resp7 = caps.clone();
        resp7.push(0x90);
        resp7.push(0x00);
        sam.push_ok(resp7);

        let result = authenticate_picc_ev2(&mut sam, &mut picc, 0x02, 0x05, 0x01).unwrap();
        assert_eq!(result.caps, caps);

        // Assert exact APDU order/content emitted by the host.
        assert_eq!(picc.sent.len(), 2);
        assert_eq!(picc.sent[0], apdu::build_picc_authenticate_ev2_first(0x02));
        assert_eq!(picc.sent[1], apdu::build_picc_continue(&pcd_chal_resp));

        assert_eq!(sam.sent.len(), 2);
        assert_eq!(
            sam.sent[0],
            apdu::build_authenticate_picc_pass1(
                apdu::AUTH_PICC_P1_EV2_FIRST,
                0x05,
                0x01,
                &ek_rnd_b
            )
        );
        assert_eq!(sam.sent[1], apdu::build_authenticate_picc_pass2(&pd_resp));
    }

    #[test]
    fn sam_6xxx_status_on_pass1_is_fail_closed_and_stops_the_handshake() {
        let ek_rnd_b = [0x11u8; 16];

        let mut picc = MockTransport::new();
        let mut resp1 = vec![apdu::PICC_AF];
        resp1.extend_from_slice(&ek_rnd_b);
        picc.push_ok(resp1);

        let mut sam = MockTransport::new();
        // SAM rejects pass 1 with a 6xxx status instead of 90AF.
        let mut resp3 = vec![0xAAu8; 32];
        resp3.push(0x6A);
        resp3.push(0x82);
        sam.push_ok(resp3);

        let err = authenticate_picc_ev2(&mut sam, &mut picc, 0x02, 0x05, 0x01).unwrap_err();
        assert!(matches!(err, SamError::Status(0x6A82)));

        // Fail-closed: the handshake must stop after step 3/4 — steps 5-8
        // never happen.
        assert_eq!(
            picc.sent.len(),
            1,
            "must not proceed to step 5 after a SAM rejection"
        );
        assert_eq!(
            sam.sent.len(),
            1,
            "must not proceed to step 7 after a SAM rejection"
        );
    }

    #[test]
    fn picc_unexpected_status_on_step2_is_fail_closed() {
        let mut picc = MockTransport::new();
        // Card returns a native error status instead of AF (correctly sized
        // at 17 bytes so the length check passes and the status check is
        // what actually rejects it).
        let mut resp1 = vec![0x7Eu8]; // arbitrary DESFire native error code
        resp1.extend_from_slice(&[0u8; 16]);
        picc.push_ok(resp1);

        let mut sam = MockTransport::new();

        let err = authenticate_picc_ev2(&mut sam, &mut picc, 0x02, 0x05, 0x01).unwrap_err();
        assert!(matches!(err, SamError::PiccStatus(0x7E)));
        assert_eq!(
            sam.sent.len(),
            0,
            "must never reach the SAM if the card itself rejects"
        );
    }

    #[test]
    fn sam_6xxx_status_on_pass2_is_fail_closed() {
        let ek_rnd_b = [0x11u8; 16];
        let pcd_chal_resp = [0x22u8; 32];
        let pd_resp = [0x33u8; 32];

        let mut picc = MockTransport::new();
        let mut resp1 = vec![apdu::PICC_AF];
        resp1.extend_from_slice(&ek_rnd_b);
        picc.push_ok(resp1);
        let mut resp5 = vec![apdu::PICC_OK];
        resp5.extend_from_slice(&pd_resp);
        picc.push_ok(resp5);

        let mut sam = MockTransport::new();
        let mut resp3 = pcd_chal_resp.to_vec();
        resp3.push(0x90);
        resp3.push(0xAF);
        sam.push_ok(resp3);
        // SAM rejects pass 2.
        let mut resp7 = vec![0u8; 2];
        resp7.push(0x69);
        resp7.push(0x82);
        sam.push_ok(resp7);

        let err = authenticate_picc_ev2(&mut sam, &mut picc, 0x02, 0x05, 0x01).unwrap_err();
        assert!(matches!(err, SamError::Status(0x6982)));
    }

    /// Build a MockTransport that will correctly play the SAM's side of
    /// `authenticate_host` for the given inputs, using the same crypto this
    /// module calls internally (self-consistent — see HARDWARE-VERIFY notes
    /// on `authenticate_host`; this proves framing/sequencing/derivation
    /// wiring, not correctness against real hardware).
    fn mock_sam_for_authenticate_host(
        master_key: &[u8; 16],
        host_mode: u8,
        rnd1: [u8; 12],
        rnd2: [u8; 12],
        rnd_b: [u8; 16],
    ) -> MockTransport {
        let mut sam = MockTransport::new();

        // Pass 1 response: rnd2 || 90 AF.
        let mut resp1 = rnd2.to_vec();
        resp1.push(0x90);
        resp1.push(0xAF);
        sam.push_ok(resp1);

        // Pass 2 response: <8 arbitrary bytes> || EncKex(RndB) || 90 AF.
        let kex = crypto::derive_kex(master_key, &rnd1, &rnd2).unwrap();
        let rnd_bc = crypto::aes128_cbc_encrypt_no_pad(&kex, &[0u8; 16], &rnd_b).unwrap();
        let mut resp2 = vec![0u8; 8];
        resp2.extend_from_slice(&rnd_bc);
        resp2.push(0x90);
        resp2.push(0xAF);
        sam.push_ok(resp2);

        // Pass 3 response: 90 00 (no payload decoded — see HARDWARE-VERIFY).
        let _ = host_mode; // host_mode only affects cmac2, computed by the SUT.
        sam.push_ok(vec![0x90, 0x00]);

        sam
    }

    #[test]
    fn authenticate_host_success_emits_three_passes_and_derives_session_keys() {
        let master_key = [0x5Au8; 16];
        let rnd1 = [0x01u8; 12];
        let rnd_a = [0x02u8; 16];
        let rnd2 = [0x03u8; 12];
        let rnd_b = [0x04u8; 16];
        let host_mode = apdu::HOST_MODE_FULL;

        let mut sam = mock_sam_for_authenticate_host(&master_key, host_mode, rnd1, rnd2, rnd_b);

        let session =
            authenticate_host(&mut sam, &master_key, 0x00, 0x00, host_mode, rnd1, rnd_a).unwrap();

        let expected = crypto::derive_host_session_keys(&master_key, &rnd_a, &rnd_b).unwrap();
        assert_eq!(session.ke, expected.ke);
        assert_eq!(session.km, expected.km);

        // Exact APDU sequence: pass1, pass2, pass3.
        assert_eq!(sam.sent.len(), 3);
        assert_eq!(
            sam.sent[0],
            apdu::build_authenticate_host_pass1(0x00, 0x00, host_mode)
        );
        let cmac2 = crypto::host_auth_pass2_cmac(&master_key, &rnd2, host_mode);
        assert_eq!(
            sam.sent[1],
            apdu::build_authenticate_host_pass2(&cmac2, &rnd1)
        );
        // Pass 3's exact bytes depend on Kex-encrypted RndA||rotl(RndB,2),
        // which is exactly what the SUT and this helper both compute the
        // same way — just check the framing (header + length), not a
        // byte-for-byte re-derivation (that would just restate the SUT).
        assert_eq!(&sam.sent[2][0..5], &[0x80, 0xA4, 0x00, 0x00, 32]);
        assert_eq!(sam.sent[2].len(), 5 + 32 + 1);
    }

    #[test]
    fn authenticate_host_pass1_bad_status_is_fail_closed() {
        let master_key = [0x5Au8; 16];
        let mut sam = MockTransport::new();
        let mut resp1 = vec![0u8; 12];
        resp1.push(0x6A);
        resp1.push(0x82);
        sam.push_ok(resp1);

        let err = authenticate_host(
            &mut sam,
            &master_key,
            0x00,
            0x00,
            apdu::HOST_MODE_FULL,
            [0u8; 12],
            [0u8; 16],
        )
        .unwrap_err();
        assert!(matches!(err, SamError::Status(0x6A82)));
        assert_eq!(sam.sent.len(), 1, "must not proceed past pass 1");
    }

    #[test]
    fn authenticate_host_pass2_bad_status_is_fail_closed() {
        let master_key = [0x5Au8; 16];
        let rnd2 = [0x03u8; 12];
        let mut sam = MockTransport::new();
        let mut resp1 = rnd2.to_vec();
        resp1.push(0x90);
        resp1.push(0xAF);
        sam.push_ok(resp1);
        let mut resp2 = vec![0u8; 24];
        resp2.push(0x6A);
        resp2.push(0x82);
        sam.push_ok(resp2);

        let err = authenticate_host(
            &mut sam,
            &master_key,
            0x00,
            0x00,
            apdu::HOST_MODE_FULL,
            [0x01u8; 12],
            [0x02u8; 16],
        )
        .unwrap_err();
        assert!(matches!(err, SamError::Status(0x6A82)));
        assert_eq!(sam.sent.len(), 2, "must not proceed past pass 2");
    }

    #[test]
    fn authenticate_host_pass3_bad_status_is_fail_closed() {
        let master_key = [0x5Au8; 16];
        let rnd1 = [0x01u8; 12];
        let rnd_a = [0x02u8; 16];
        let rnd2 = [0x03u8; 12];
        let rnd_b = [0x04u8; 16];
        // Built manually (not via `mock_sam_for_authenticate_host`) so pass
        // 3's response can be swapped for a failure status.
        let mut sam = MockTransport::new();
        let mut resp1 = rnd2.to_vec();
        resp1.push(0x90);
        resp1.push(0xAF);
        sam.push_ok(resp1);
        let kex = crypto::derive_kex(&master_key, &rnd1, &rnd2).unwrap();
        let rnd_bc = crypto::aes128_cbc_encrypt_no_pad(&kex, &[0u8; 16], &rnd_b).unwrap();
        let mut resp2 = vec![0u8; 8];
        resp2.extend_from_slice(&rnd_bc);
        resp2.push(0x90);
        resp2.push(0xAF);
        sam.push_ok(resp2);
        sam.push_ok(vec![0x91, 0xAE]); // pass 3 rejected

        let err = authenticate_host(
            &mut sam,
            &master_key,
            0x00,
            0x00,
            apdu::HOST_MODE_FULL,
            rnd1,
            rnd_a,
        )
        .unwrap_err();
        assert!(matches!(err, SamError::Status(0x91AE)));
        assert_eq!(sam.sent.len(), 3);
    }

    // -------------------------------------------------------------
    // lock_unlock_unlock (SAM_LockUnlock, state=Unlock).
    // -------------------------------------------------------------

    /// Build a MockTransport that plays back a self-consistent SAM side of
    /// `lock_unlock_unlock` for the given inputs — proves framing/sequencing
    /// wiring, not correctness against real hardware (see HARDWARE-VERIFY
    /// note on `lock_unlock_unlock`). Mirrors
    /// [`mock_sam_for_authenticate_host`] but keyed on `state` instead of
    /// `host_mode` and using `INS 0x10` framing for pass 3.
    fn mock_sam_for_lock_unlock(
        master_key: &[u8; 16],
        rnd2: [u8; 12],
        rnd1: [u8; 12],
        rnd_b: [u8; 16],
    ) -> MockTransport {
        let mut sam = MockTransport::new();
        let state = apdu::LOCK_UNLOCK_STATE_UNLOCK;

        // Pass 1 response: rnd2 || 90 AF.
        let mut resp1 = rnd2.to_vec();
        resp1.push(0x90);
        resp1.push(0xAF);
        sam.push_ok(resp1);

        // Pass 2 response: macSam[8] (SAM's own CMAC over rnd1||state||000)
        // || EncKex(RndB)[16] || 90 AF.
        let mac_sam = crypto::host_auth_pass2_cmac(master_key, &rnd1, state);
        let kex = crypto::derive_kex(master_key, &rnd1, &rnd2).unwrap();
        let rnd_bc = crypto::aes128_cbc_encrypt_no_pad(&kex, &[0u8; 16], &rnd_b).unwrap();
        let mut resp2 = mac_sam.to_vec();
        resp2.extend_from_slice(&rnd_bc);
        resp2.push(0x90);
        resp2.push(0xAF);
        sam.push_ok(resp2);

        // Pass 3 response: 90 00 (no payload decoded — see HARDWARE-VERIFY).
        sam.push_ok(vec![0x90, 0x00]);

        sam
    }

    #[test]
    fn lock_unlock_unlock_success_emits_three_passes_with_correct_apdus() {
        let master_key = [0x5Au8; 16];
        let rnd1 = [0x01u8; 12];
        let rnd_a = [0x02u8; 16];
        let rnd2 = [0x03u8; 12];
        let rnd_b = [0x04u8; 16];

        let mut sam = mock_sam_for_lock_unlock(&master_key, rnd2, rnd1, rnd_b);

        let session = lock_unlock_unlock(&mut sam, 0x00, 0x00, &master_key, rnd1, rnd_a).unwrap();

        let expected = crypto::derive_host_session_keys(&master_key, &rnd_a, &rnd_b).unwrap();
        assert_eq!(
            session.ke, expected.ke,
            "lock_unlock_unlock must return the session it just established, \
             derived exactly like authenticate_host's"
        );
        assert_eq!(session.km, expected.km);

        assert_eq!(sam.sent.len(), 3, "pass1 + pass2 + pass3");
        assert_eq!(
            sam.sent[0],
            apdu::build_lock_unlock_pass1(apdu::LOCK_UNLOCK_STATE_UNLOCK, 0x00, 0x00)
        );
        let mac_host =
            crypto::host_auth_pass2_cmac(&master_key, &rnd2, apdu::LOCK_UNLOCK_STATE_UNLOCK);
        assert_eq!(sam.sent[1], apdu::build_lock_unlock_pass2(&mac_host, &rnd1));

        // Pass 3's exact bytes depend on Kex-encrypted RndA||rotl(RndB,2),
        // which is exactly what the SUT and this helper both compute the
        // same way — just check the framing (INS 0x10 header + length), not
        // a byte-for-byte re-derivation (that would just restate the SUT).
        assert_eq!(&sam.sent[2][0..5], &[0x80, 0x10, 0x00, 0x00, 32]);
        assert_eq!(sam.sent[2].len(), 5 + 32 + 1);
    }

    #[test]
    fn lock_unlock_unlock_pass1_bad_status_is_fail_closed() {
        let master_key = [0x5Au8; 16];
        let mut sam = MockTransport::new();
        let mut resp1 = vec![0u8; 12];
        resp1.push(0x69);
        resp1.push(0x85); // e.g. already unlocked / conditions not satisfied
        sam.push_ok(resp1);

        let err = lock_unlock_unlock(&mut sam, 0x00, 0x00, &master_key, [0u8; 12], [0u8; 16])
            .unwrap_err();
        assert!(matches!(err, SamError::Status(0x6985)));
        assert_eq!(sam.sent.len(), 1, "must not proceed past pass 1");
    }

    #[test]
    fn lock_unlock_unlock_pass2_bad_status_is_fail_closed() {
        let master_key = [0x5Au8; 16];
        let rnd2 = [0x03u8; 12];
        let mut sam = MockTransport::new();
        let mut resp1 = rnd2.to_vec();
        resp1.push(0x90);
        resp1.push(0xAF);
        sam.push_ok(resp1);
        let mut resp2 = vec![0u8; 24];
        resp2.push(0x6A);
        resp2.push(0x82);
        sam.push_ok(resp2);

        let err = lock_unlock_unlock(
            &mut sam,
            0x00,
            0x00,
            &master_key,
            [0x01u8; 12],
            [0x02u8; 16],
        )
        .unwrap_err();
        assert!(matches!(err, SamError::Status(0x6A82)));
        assert_eq!(sam.sent.len(), 2, "must not silently retry");
    }

    #[test]
    fn lock_unlock_unlock_wrong_pass2_cmac_is_fail_closed() {
        let master_key = [0x5Au8; 16];
        let rnd1 = [0x01u8; 12];
        let rnd_a = [0x02u8; 16];
        let rnd2 = [0x03u8; 12];

        let mut sam = MockTransport::new();
        let mut resp1 = rnd2.to_vec();
        resp1.push(0x90);
        resp1.push(0xAF);
        sam.push_ok(resp1);

        // Pass 2 response carries a bogus macSam that does NOT match the
        // host's own CMAC computation over rnd1||state||000.
        let mut resp2 = vec![0xEEu8; 8];
        resp2.extend_from_slice(&[0u8; 16]);
        resp2.push(0x90);
        resp2.push(0xAF);
        sam.push_ok(resp2);

        let err = lock_unlock_unlock(&mut sam, 0x00, 0x00, &master_key, rnd1, rnd_a).unwrap_err();
        assert!(matches!(err, SamError::CmacMismatch(_)));
        assert_eq!(
            sam.sent.len(),
            2,
            "must not proceed to pass 3 after a CMAC mismatch"
        );
    }

    #[test]
    fn lock_unlock_unlock_pass3_bad_status_is_fail_closed() {
        let master_key = [0x5Au8; 16];
        let rnd1 = [0x01u8; 12];
        let rnd_a = [0x02u8; 16];
        let rnd2 = [0x03u8; 12];
        let rnd_b = [0x04u8; 16];
        let state = apdu::LOCK_UNLOCK_STATE_UNLOCK;

        // Built manually (not via `mock_sam_for_lock_unlock`) so pass 3's
        // response can be swapped for a failure status.
        let mut sam = MockTransport::new();
        let mut resp1 = rnd2.to_vec();
        resp1.push(0x90);
        resp1.push(0xAF);
        sam.push_ok(resp1);

        let mac_sam = crypto::host_auth_pass2_cmac(&master_key, &rnd1, state);
        let kex = crypto::derive_kex(&master_key, &rnd1, &rnd2).unwrap();
        let rnd_bc = crypto::aes128_cbc_encrypt_no_pad(&kex, &[0u8; 16], &rnd_b).unwrap();
        let mut resp2 = mac_sam.to_vec();
        resp2.extend_from_slice(&rnd_bc);
        resp2.push(0x90);
        resp2.push(0xAF);
        sam.push_ok(resp2);
        sam.push_ok(vec![0x91, 0xAE]); // pass 3 rejected

        let err = lock_unlock_unlock(&mut sam, 0x00, 0x00, &master_key, rnd1, rnd_a).unwrap_err();
        assert!(matches!(err, SamError::Status(0x91AE)));
        assert_eq!(sam.sent.len(), 3);
    }

    // -------------------------------------------------------------
    // send_via_sm (Apply_SM / transceive / Remove_SM relay).
    // -------------------------------------------------------------

    #[test]
    fn send_via_sm_relays_apply_transceive_remove_in_order() {
        let mut sam = MockTransport::new();
        let mut picc = MockTransport::new();

        let mut apply_resp = vec![0xAA, 0xBB]; // "wrapped command" bytes
        apply_resp.push(0x90);
        apply_resp.push(0x00);
        sam.push_ok(apply_resp);

        picc.push_ok(vec![0x00, 0xCC]); // card's raw response

        let mut remove_resp = vec![0xDD, 0xEE]; // final plain data
        remove_resp.push(0x90);
        remove_resp.push(0x00);
        sam.push_ok(remove_resp);

        let cmd = vec![0xCA, 0x01, 0x02, 0x03, 0x00, 0x20];
        let data = send_via_sm(&mut sam, &mut picc, apdu::COMM_MODE_PLAIN, &cmd).unwrap();
        assert_eq!(data, vec![0xDD, 0xEE]);

        assert_eq!(sam.sent.len(), 2);
        assert_eq!(
            sam.sent[0],
            apdu::build_apply_sm(apdu::COMM_MODE_PLAIN, &cmd)
        );
        assert_eq!(sam.sent[1], apdu::build_remove_sm(&[0x00, 0xCC]));

        assert_eq!(picc.sent.len(), 1);
        assert_eq!(picc.sent[0], vec![0xAA, 0xBB]);
    }

    #[test]
    fn send_via_sm_fails_closed_on_apply_sm_rejection_never_touches_picc() {
        let mut sam = MockTransport::new();
        let mut picc = MockTransport::new();

        sam.push_ok(vec![0x6A, 0x82]);

        let err = send_via_sm(&mut sam, &mut picc, apdu::COMM_MODE_FULL, &[0x3D]).unwrap_err();
        assert!(matches!(err, SamError::Status(0x6A82)));
        assert_eq!(sam.sent.len(), 1);
        assert_eq!(
            picc.sent.len(),
            0,
            "must never reach the card if Apply_SM fails"
        );
    }

    #[test]
    fn send_via_sm_fails_closed_on_picc_transport_error_never_calls_remove_sm() {
        let mut sam = MockTransport::new();
        let mut picc = MockTransport::new();

        let mut apply_resp = vec![0xAA];
        apply_resp.push(0x90);
        apply_resp.push(0x00);
        sam.push_ok(apply_resp);
        picc.push_err(SamError::Transport("card removed mid-write".into()));

        let err = send_via_sm(&mut sam, &mut picc, apdu::COMM_MODE_FULL, &[0x3D]).unwrap_err();
        assert!(matches!(err, SamError::Transport(_)));
        assert_eq!(
            sam.sent.len(),
            1,
            "must not call Remove_SM after a PICC failure"
        );
        assert_eq!(picc.sent.len(), 1);
    }

    #[test]
    fn send_via_sm_fails_closed_on_remove_sm_rejection() {
        let mut sam = MockTransport::new();
        let mut picc = MockTransport::new();

        let mut apply_resp = vec![0xAA];
        apply_resp.push(0x90);
        apply_resp.push(0x00);
        sam.push_ok(apply_resp);
        picc.push_ok(vec![0x00]);
        sam.push_ok(vec![0x91, 0x1C]); // Remove_SM rejects (e.g. MAC mismatch)

        let err = send_via_sm(&mut sam, &mut picc, apdu::COMM_MODE_FULL, &[0x3D]).unwrap_err();
        assert!(matches!(err, SamError::Status(0x911C)));
        assert_eq!(sam.sent.len(), 2);
        assert_eq!(picc.sent.len(), 1);
    }

    // -------------------------------------------------------------
    // authenticate_picc_ev2_diversified (bead bi0).
    // -------------------------------------------------------------

    #[test]
    fn diversified_handshake_success_uses_diversified_pass1_builder() {
        let ek_rnd_b = [0x11u8; 16];
        let pcd_chal_resp = [0x22u8; 32];
        let pd_resp = [0x33u8; 32];
        let caps = vec![0xCA, 0xFE];
        let div_input = [0x99u8; 11];

        let mut picc = MockTransport::new();
        let mut resp1 = vec![apdu::PICC_AF];
        resp1.extend_from_slice(&ek_rnd_b);
        picc.push_ok(resp1);
        let mut resp5 = vec![apdu::PICC_OK];
        resp5.extend_from_slice(&pd_resp);
        picc.push_ok(resp5);

        let mut sam = MockTransport::new();
        let mut resp3 = pcd_chal_resp.to_vec();
        resp3.push(0x90);
        resp3.push(0xAF);
        sam.push_ok(resp3);
        let mut resp7 = caps.clone();
        resp7.push(0x90);
        resp7.push(0x00);
        sam.push_ok(resp7);

        let result =
            authenticate_picc_ev2_diversified(&mut sam, &mut picc, 0x00, 0x05, 0x01, &div_input)
                .unwrap();
        assert_eq!(result.caps, caps);

        // The diversified pass-1 APDU (P1 differs, DivInput appended) is
        // what actually went out, not the plain builder's output.
        assert_eq!(
            sam.sent[0],
            apdu::build_authenticate_picc_pass1_diversified(0x05, 0x01, &ek_rnd_b, &div_input)
        );
        assert_ne!(
            sam.sent[0],
            apdu::build_authenticate_picc_pass1(
                apdu::AUTH_PICC_P1_EV2_FIRST,
                0x05,
                0x01,
                &ek_rnd_b
            ),
            "a diversified auth must not send the same pass-1 bytes as a non-diversified one"
        );
    }

    #[test]
    fn diversified_handshake_sam_rejection_is_fail_closed() {
        let ek_rnd_b = [0x11u8; 16];
        let div_input = [0x01u8; 11];

        let mut picc = MockTransport::new();
        let mut resp1 = vec![apdu::PICC_AF];
        resp1.extend_from_slice(&ek_rnd_b);
        picc.push_ok(resp1);

        let mut sam = MockTransport::new();
        // SAM rejects — e.g. div_input doesn't match the key on the card
        // (a foreign/cloned/wrong card), exactly like a bad key-entry would.
        let mut resp3 = vec![0xAAu8; 32];
        resp3.push(0x6A);
        resp3.push(0x82);
        sam.push_ok(resp3);

        let err =
            authenticate_picc_ev2_diversified(&mut sam, &mut picc, 0x00, 0x05, 0x01, &div_input)
                .unwrap_err();
        assert!(matches!(err, SamError::Status(0x6A82)));
        assert_eq!(picc.sent.len(), 1, "must not proceed to step 5");
        assert_eq!(sam.sent.len(), 1, "must not proceed to step 7");
    }

    // -------------------------------------------------------------
    // read_token_id (bead bi0).
    // -------------------------------------------------------------

    #[test]
    fn read_token_id_happy_path_returns_exact_length_data() {
        let mut sam = MockTransport::new();
        let mut picc = MockTransport::new();
        let token_id = vec![0xABu8; 16];

        let mut apply_resp = vec![0xAA, 0xBB];
        apply_resp.push(0x90);
        apply_resp.push(0x00);
        sam.push_ok(apply_resp);
        picc.push_ok(vec![0x00, 0xCC]);
        let mut remove_resp = token_id.clone();
        remove_resp.push(0x90);
        remove_resp.push(0x00);
        sam.push_ok(remove_resp);

        let data = read_token_id(&mut sam, &mut picc, 0x00, 16).unwrap();
        assert_eq!(data, token_id);

        let expected_read_cmd = apdu::build_picc_read_data(0x00, 0, 16);
        assert_eq!(
            sam.sent[0],
            apdu::build_apply_sm(apdu::COMM_MODE_FULL, &expected_read_cmd)
        );
    }

    #[test]
    fn read_token_id_wrong_length_is_fail_closed() {
        let mut sam = MockTransport::new();
        let mut picc = MockTransport::new();

        let mut apply_resp = vec![0xAA];
        apply_resp.push(0x90);
        apply_resp.push(0x00);
        sam.push_ok(apply_resp);
        picc.push_ok(vec![0x00]);
        // Card/SAM hand back only 8 bytes where 16 were expected.
        let mut remove_resp = vec![0x11u8; 8];
        remove_resp.push(0x90);
        remove_resp.push(0x00);
        sam.push_ok(remove_resp);

        let err = read_token_id(&mut sam, &mut picc, 0x00, 16).unwrap_err();
        assert!(matches!(
            err,
            SamError::UnexpectedLength {
                got: 8,
                expected: 16
            }
        ));
    }

    #[test]
    fn read_token_id_apply_sm_rejection_is_fail_closed_never_touches_picc() {
        let mut sam = MockTransport::new();
        let mut picc = MockTransport::new();
        sam.push_ok(vec![0x6A, 0x82]);

        let err = read_token_id(&mut sam, &mut picc, 0x00, 16).unwrap_err();
        assert!(matches!(err, SamError::Status(0x6A82)));
        assert_eq!(picc.sent.len(), 0);
    }

    // -------------------------------------------------------------
    // read_token_id_software (bead bi0 rework 2026-07-15).
    // -------------------------------------------------------------

    /// Encrypt `plain` the way the CARD would for a `CommMode.Full`
    /// response — same ISO/IEC 9797-1 method-2 pad as `encrypt_full`, but
    /// under [`crypto::DesfireEv2Session::response_iv`] (the PICC -> PCD IV,
    /// which deliberately differs from the PCD -> PICC `command_iv` that
    /// `encrypt_full` uses) — mirrors `crypto::tests::encrypt_as_response`,
    /// duplicated here since that helper is private to `crypto`'s own test
    /// module.
    fn encrypt_as_response(session: &crypto::DesfireEv2Session, plain: &[u8]) -> Vec<u8> {
        let mut padded = plain.to_vec();
        padded.push(0x80);
        while !padded.len().is_multiple_of(16) {
            padded.push(0x00);
        }
        let iv = session.response_iv().unwrap();
        crypto::aes128_cbc_encrypt_no_pad(&session.enc_key, &iv, &padded).unwrap()
    }

    /// Queue a crypto-self-consistent `ReadData` `CommMode.Full` response
    /// (`<status> || EncData || CMACt(8)`) for `session`, returning the
    /// plaintext it decrypts to (`plain_data`, exactly as
    /// `read_token_id_software` should return it).
    ///
    /// `session` is passed at its CURRENT counter `N` (the value
    /// `read_token_id_software` will see when it's called), but the card's
    /// response MAC/IV are built at `N+1` — HW-confirmed 2026-07-15 (bead
    /// 3l1): the card increments CmdCtr BETWEEN command and response, and
    /// `read_token_id_software` advances `session` to `N+1` before verifying
    /// the response. Building the response at `N` instead would make this
    /// mock diverge from a real card and mask the bug this fix corrected.
    fn push_read_data_ok(
        picc: &mut MockTransport,
        session: &crypto::DesfireEv2Session,
        plain_data: &[u8],
    ) {
        let response_session = crypto::DesfireEv2Session {
            enc_key: session.enc_key,
            mac_key: session.mac_key,
            ti: session.ti,
            cmd_ctr: session.cmd_ctr.wrapping_add(1),
        };
        let enc_data = encrypt_as_response(&response_session, plain_data);
        let mac = response_session.mac_response(apdu::PICC_OK, &enc_data);
        let mut resp = vec![apdu::PICC_OK];
        resp.extend_from_slice(&enc_data);
        resp.extend_from_slice(&mac);
        picc.push_ok(resp);
    }

    #[test]
    fn read_token_id_software_happy_path_returns_exact_length_data() {
        let mut picc = MockTransport::new();
        let mut session = crypto::DesfireEv2Session::new([0x11u8; 16], [0x22u8; 16], [1, 2, 3, 4]);
        let token_id = [0xABu8; 16];
        push_read_data_ok(&mut picc, &session, &token_id);

        let data = read_token_id_software(&mut picc, &mut session, 0x00, 16).unwrap();
        assert_eq!(data, token_id.to_vec());
        assert_eq!(session.cmd_ctr, 1, "must advance the counter on success");

        let expected_read_cmd = apdu::build_picc_read_data(0x00, 0, 16);
        assert_eq!(picc.sent.len(), 1);
        assert_eq!(
            &picc.sent[0][..expected_read_cmd.len()],
            &expected_read_cmd[..]
        );
    }

    #[test]
    fn read_token_id_software_fails_closed_on_native_status_rejection() {
        let mut picc = MockTransport::new();
        let mut session = crypto::DesfireEv2Session::new([0x11u8; 16], [0x22u8; 16], [1, 2, 3, 4]);
        // Card rejects with a native error status instead of PICC_OK.
        picc.push_ok(vec![0x7Eu8]);

        let err = read_token_id_software(&mut picc, &mut session, 0x00, 16).unwrap_err();
        assert!(matches!(err, SamError::PiccStatus(0x7E)));
        assert_eq!(session.cmd_ctr, 0, "must not advance on failure");
    }

    #[test]
    fn read_token_id_software_fails_closed_on_response_mac_mismatch() {
        let mut picc = MockTransport::new();
        let mut session = crypto::DesfireEv2Session::new([0x11u8; 16], [0x22u8; 16], [1, 2, 3, 4]);
        let enc_data = session.encrypt_full(&[0xABu8; 16]).unwrap();
        // Correct EncData, but a bogus (tampered/wrong) trailing MAC.
        let mut resp = vec![apdu::PICC_OK];
        resp.extend_from_slice(&enc_data);
        resp.extend_from_slice(&[0xEEu8; 8]);
        picc.push_ok(resp);

        let err = read_token_id_software(&mut picc, &mut session, 0x00, 16).unwrap_err();
        assert!(matches!(err, SamError::CmacMismatch(_)));
        // The card accepted the command (native status PICC_OK) BEFORE
        // returning the bad MAC, so the counter legitimately advances here —
        // we still refuse to trust the (unverified) data itself.
        assert_eq!(session.cmd_ctr, 1);
    }

    #[test]
    fn read_token_id_software_fails_closed_on_wrong_length_read() {
        let mut picc = MockTransport::new();
        let mut session = crypto::DesfireEv2Session::new([0x11u8; 16], [0x22u8; 16], [1, 2, 3, 4]);
        // The card/session honestly encodes only 4 bytes where 16 were
        // expected — decrypts fine, but to the wrong length.
        push_read_data_ok(&mut picc, &session, &[0x11u8; 4]);

        let err = read_token_id_software(&mut picc, &mut session, 0x00, 16).unwrap_err();
        assert!(matches!(
            err,
            SamError::UnexpectedLength {
                got: 4,
                expected: 16
            }
        ));
    }

    #[test]
    fn read_token_id_software_fails_closed_on_response_too_short_for_mac() {
        let mut picc = MockTransport::new();
        let mut session = crypto::DesfireEv2Session::new([0x11u8; 16], [0x22u8; 16], [1, 2, 3, 4]);
        // Only 4 bytes after the status byte — too short to carry an 8-byte MAC.
        picc.push_ok(vec![apdu::PICC_OK, 0x01, 0x02, 0x03, 0x04]);

        let err = read_token_id_software(&mut picc, &mut session, 0x00, 16).unwrap_err();
        assert!(matches!(
            err,
            SamError::UnexpectedLength {
                got: 4,
                expected: 8
            }
        ));
    }

    // -------------------------------------------------------------
    // authenticate_picc_ev2_software (bead 4ty rework 2026-07-15).
    // -------------------------------------------------------------

    /// Queue a crypto-self-consistent (proves wiring, not hardware
    /// correctness — same caveat as `mock_sam_for_authenticate_host`) native
    /// PICC response pair for `authenticate_picc_ev2_software`. Returns the
    /// `ti` the SUT should recover, so tests can assert the returned
    /// session matches.
    fn push_software_ev2_ok(
        picc: &mut MockTransport,
        key: &[u8; 16],
        rnd_a: [u8; 16],
        rnd_b: [u8; 16],
    ) -> [u8; 4] {
        // Step 2 response: AF || Ek,IV=0(RndB).
        let ek_rnd_b = crypto::aes128_cbc_encrypt_no_pad(key, &[0u8; 16], &rnd_b).unwrap();
        let mut resp1 = vec![apdu::PICC_AF];
        resp1.extend_from_slice(&ek_rnd_b);
        picc.push_ok(resp1);

        // DESFire EV2 resets the IV to zero per handshake message, so the
        // card's step-5 response is encrypted under IV=0 (matching the SUT).
        let ti = [0xAAu8, 0xBB, 0xCC, 0xDD];
        let rnd_a_rot = crypto::rotate_left_16(&rnd_a, 1);
        let mut plain5 = Vec::with_capacity(32);
        plain5.extend_from_slice(&ti);
        plain5.extend_from_slice(&rnd_a_rot);
        plain5.extend_from_slice(&[0u8; 6]); // PDcap2
        plain5.extend_from_slice(&[0u8; 6]); // PCDcap2
        let cipher5 = crypto::aes128_cbc_encrypt_no_pad(key, &[0u8; 16], &plain5).unwrap();
        let mut resp4 = vec![apdu::PICC_OK];
        resp4.extend_from_slice(&cipher5);
        picc.push_ok(resp4);

        ti
    }

    #[test]
    fn authenticate_picc_ev2_software_success_derives_session_and_ti() {
        let key = [0x00u8; 16]; // factory all-zero AES app key
        let rnd_a = [0x11u8; 16];
        let rnd_b = [0x22u8; 16];

        let mut picc = MockTransport::new();
        let expected_ti = push_software_ev2_ok(&mut picc, &key, rnd_a, rnd_b);

        let result = authenticate_picc_ev2_software(&mut picc, 0x00, &key, rnd_a).unwrap();
        assert_eq!(result.session.ti, expected_ti);
        assert_eq!(result.session.cmd_ctr, 0);

        let (expected_enc, expected_mac) =
            crypto::derive_desfire_ev2_session_keys(&key, &rnd_a, &rnd_b);
        assert_eq!(result.session.enc_key, expected_enc);
        assert_eq!(result.session.mac_key, expected_mac);

        assert_eq!(picc.sent.len(), 2);
        assert_eq!(picc.sent[0], apdu::build_picc_authenticate_ev2_first(0x00));
    }

    #[test]
    fn authenticate_picc_ev2_software_rejects_card_native_error_at_step2() {
        let key = [0x00u8; 16];
        let mut picc = MockTransport::new();
        // Card returns a native error status instead of AF.
        let mut resp1 = vec![0x7Eu8];
        resp1.extend_from_slice(&[0u8; 16]);
        picc.push_ok(resp1);

        let err = authenticate_picc_ev2_software(&mut picc, 0x00, &key, [0x11u8; 16]).unwrap_err();
        assert!(matches!(err, SamError::PiccStatus(0x7E)));
        assert_eq!(picc.sent.len(), 1, "must not proceed to step 4");
    }

    #[test]
    fn authenticate_picc_ev2_software_fails_closed_on_rnd_a_prime_mismatch() {
        let key = [0x00u8; 16];
        let rnd_a = [0x11u8; 16];
        let rnd_b = [0x22u8; 16];

        let mut picc = MockTransport::new();
        // Correct step-2 response, but a step-5 response encrypted with the
        // WRONG rnd_a rotation baked in (simulates a relay/impersonation
        // attempt or a session mismatch) — the SUT must reject it, not
        // silently accept a session.
        let ek_rnd_b = crypto::aes128_cbc_encrypt_no_pad(&key, &[0u8; 16], &rnd_b).unwrap();
        let mut resp1 = vec![apdu::PICC_AF];
        resp1.extend_from_slice(&ek_rnd_b);
        picc.push_ok(resp1);

        let rnd_b_rot = crypto::rotate_left_16(&rnd_b, 1);
        let mut plain4 = Vec::with_capacity(32);
        plain4.extend_from_slice(&rnd_a);
        plain4.extend_from_slice(&rnd_b_rot);
        let cipher4 = crypto::aes128_cbc_encrypt_no_pad(&key, &[0u8; 16], &plain4).unwrap();
        let mut chained_iv = [0u8; 16];
        chained_iv.copy_from_slice(&cipher4[16..32]);

        let wrong_rnd_a_rot = [0xFFu8; 16]; // not rotate_left_16(rnd_a, 1)
        let mut plain5 = Vec::with_capacity(32);
        plain5.extend_from_slice(&[0xAAu8, 0xBB, 0xCC, 0xDD]);
        plain5.extend_from_slice(&wrong_rnd_a_rot);
        plain5.extend_from_slice(&[0u8; 12]);
        let cipher5 = crypto::aes128_cbc_encrypt_no_pad(&key, &chained_iv, &plain5).unwrap();
        let mut resp4 = vec![apdu::PICC_OK];
        resp4.extend_from_slice(&cipher5);
        picc.push_ok(resp4);

        let err = authenticate_picc_ev2_software(&mut picc, 0x00, &key, rnd_a).unwrap_err();
        assert!(matches!(err, SamError::CmacMismatch(_)));
    }

    // -------------------------------------------------------------
    // dump_secret_key (bead 4ty rework 2026-07-15).
    // -------------------------------------------------------------

    #[test]
    fn dump_secret_key_happy_path_returns_16_byte_key() {
        let ke = [0x01u8; 16];
        let km = [0x02u8; 16];
        let host_session = HostSessionKeys { ke, km };
        let diversified_key = [0x5Au8; 16];

        // Full-protection round trip: response verified at CmdCtr=1, zero IVs
        // — see `crypto::SecureMessaging::full_protection_round_trip`.
        let response = super_full_protection_response(&ke, &km, &diversified_key, [0x90, 0x00]);
        let mut sam = MockTransport::new();
        sam.push_ok(response);

        let key = dump_secret_key(&mut sam, &host_session, 0x02, 0x00, &[0xAAu8; 11]).unwrap();
        assert_eq!(key, diversified_key);
        assert_eq!(sam.sent.len(), 1);
    }

    #[test]
    fn dump_secret_key_fails_closed_on_bad_status() {
        let ke = [0x01u8; 16];
        let km = [0x02u8; 16];
        let host_session = HostSessionKeys { ke, km };

        let response = super_full_protection_response(&ke, &km, &[0x00u8; 16], [0x6A, 0x82]);
        let mut sam = MockTransport::new();
        sam.push_ok(response);

        let err = dump_secret_key(&mut sam, &host_session, 0x02, 0x00, &[0xAAu8; 11]).unwrap_err();
        assert!(matches!(err, SamError::Status(0x6A82)));
    }

    /// Local re-implementation of `crypto::tests::mock_sam_full_protection_response`
    /// (that helper is private to `crypto`'s own test module) — builds a
    /// Full-protection SAM response the way the real AV3 SAM does, for the
    /// `dump_secret_key` tests above: response at `CmdCtr = 1` (request 0, +1),
    /// zero IVs, big-endian counter,
    /// `MAC8 = truncate_odd(CMAC(Km, IV=0, SW || CmdCtr_BE || encData))`.
    /// Self-consistent only.
    fn super_full_protection_response(
        ke: &[u8; 16],
        km: &[u8; 16],
        plain_data: &[u8],
        sw: [u8; 2],
    ) -> Vec<u8> {
        let ctr_be = 1u32.to_be_bytes();

        let mut padded = plain_data.to_vec();
        padded.push(0x80);
        while padded.len() % 16 != 0 {
            padded.push(0x00);
        }
        let mut my_iv = vec![0x02u8; 4];
        for _ in 1..4 {
            my_iv.extend_from_slice(&ctr_be);
        }
        let iv: [u8; 16] = crypto::aes128_cbc_encrypt_no_pad(ke, &[0u8; 16], &my_iv)
            .unwrap()
            .try_into()
            .unwrap();
        let enc_data = crypto::aes128_cbc_encrypt_no_pad(ke, &iv, &padded).unwrap();

        let mut mac_input = Vec::new();
        mac_input.extend_from_slice(&sw);
        mac_input.extend_from_slice(&ctr_be);
        mac_input.extend_from_slice(&enc_data);
        let mac8 =
            crypto::truncate_mac_odd(&crypto::cmac_aes128_with_iv(km, &[0u8; 16], &mac_input));

        let mut wire = enc_data;
        wire.extend_from_slice(&mac8);
        wire.extend_from_slice(&sw);
        wire
    }
}
