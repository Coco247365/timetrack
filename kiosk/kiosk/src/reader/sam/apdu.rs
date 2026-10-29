//! Byte-accurate APDU builders and response parsers for the SAM AV3
//! (S-mode, host-mediated) command set used by this project, plus the
//! DESFire-native card-side frames needed for the `SAM_AuthenticatePICC`
//! EV2 handshake.
//!
//! Every function here is pure (no I/O) so it is directly unit-testable.
//! Actual transmission goes through [`super::transport::SamTransport`] /
//! [`super::transport::PiccTransport`].
//!
//! CLA is `0x80` for every SAM AV3 command (S-mode).

use super::crypto;
use super::error::SamError;

/// CLA byte for all SAM AV3 S-mode commands.
pub const CLA: u8 = 0x80;

// ---------------------------------------------------------------------
// SAM_AuthenticateHost (INS A4) — 3-pass host<->SAM mutual authentication.
// ---------------------------------------------------------------------

pub const INS_AUTHENTICATE_HOST: u8 = 0xA4;

/// `hostMode` value for full (encrypt + MAC) protection of the host session.
pub const HOST_MODE_FULL: u8 = 0x02;

/// `hostMode` value for no protection of the host session (status/plain).
pub const HOST_MODE_PLAIN: u8 = 0x00;
/// `hostMode` value for MAC-only protection of the host session.
pub const HOST_MODE_MAC: u8 = 0x01;

/// Build pass 1 of `SAM_AuthenticateHost`:
/// `80 A4 00 00 03 <keyNo> <keyVer> <hostMode>` with `Le = 00`.
///
/// Passes 2 and 3 (below) complete the handshake. Their wire layout and the
/// Ke/Km derivation are aligned to the open-source reference implementation
/// `github.com/dumacp/smartcard` (`nxp/mifare/samav2`, `AuthHostAV2`), which
/// is exercised against real AV2/AV3 hardware — see the module-level
/// `HARDWARE-VERIFY` notes on [`parse_authenticate_host_pass1_response`],
/// [`parse_authenticate_host_pass2_response`], and
/// [`super::crypto::derive_host_session_keys`] for exactly what is and isn't
/// confirmed against this project's own SAM AV3 + DS3235 spec (bead 3pn).
pub fn build_authenticate_host_pass1(key_no: u8, key_ver: u8, host_mode: u8) -> Vec<u8> {
    vec![
        CLA,
        INS_AUTHENTICATE_HOST,
        0x00,
        0x00,
        0x03,
        key_no,
        key_ver,
        host_mode,
        0x00,
    ]
}

/// Parse the SAM's response to pass 1: `RndB'[12] || 90 AF`. `90AF` means
/// "continue" (more data expected in pass 2); any other status word —
/// including any `6xxx` SAM error — is a hard failure.
///
/// HARDWARE-VERIFY: the reference implementation reads a 12-byte (not
/// 16-byte) leading field here, i.e. a truncated/partial challenge rather
/// than a full AES block. This is unconfirmed against the DS3235 spec text
/// (which we do not have unrestricted access to) but is exactly what a
/// hardware-exercised open-source client does — see module doc on
/// [`build_authenticate_host_pass1`].
pub fn parse_authenticate_host_pass1_response(resp: &[u8]) -> Result<[u8; 12], SamError> {
    if resp.len() != 14 {
        return Err(SamError::UnexpectedLength {
            got: resp.len(),
            expected: 14,
        });
    }
    let sw = u16::from_be_bytes([resp[12], resp[13]]);
    if sw != 0x90AF {
        return Err(SamError::Status(sw));
    }
    let mut out = [0u8; 12];
    out.copy_from_slice(&resp[..12]);
    Ok(out)
}

/// Build pass 2 of `SAM_AuthenticateHost`:
/// `80 A4 00 00 14 <cmac2[8]> <rnd1[12]>` with `Le = 00`.
///
/// `cmac2` is the odd-indexed-byte truncation of
/// `CMAC(hostMasterKey, RndB'[12] || hostMode || 00 00 00)` — see
/// [`super::crypto::host_auth_pass2_cmac`]. `rnd1` is a fresh 12-byte host
/// random.
pub fn build_authenticate_host_pass2(cmac2: &[u8; 8], rnd1: &[u8; 12]) -> Vec<u8> {
    let mut apdu = Vec::with_capacity(5 + 20 + 1);
    apdu.push(CLA);
    apdu.push(INS_AUTHENTICATE_HOST);
    apdu.push(0x00);
    apdu.push(0x00);
    apdu.push(0x14); // Lc = 8 (cmac2) + 12 (rnd1)
    apdu.extend_from_slice(cmac2);
    apdu.extend_from_slice(rnd1);
    apdu.push(0x00); // Le
    apdu
}

/// Parse the SAM's response to pass 2: `<8 bytes> || EncKex(RndB)[16] || 90 AF`.
/// `90AF` means "continue" (pass 3 still expected); any other status word is
/// a hard failure.
///
/// HARDWARE-VERIFY: the leading 8 bytes are **not decoded or verified** here
/// — the reference implementation this is aligned to discards them too. If
/// DS3235 defines them as a MAC/authenticator over the host's pass-2 data,
/// skipping that check is a real (inherited) weakening of pass-2's
/// server-authenticates-client direction; the overall handshake's security
/// still rests on the SAM having accepted `cmac2` (or it would have returned
/// an error status instead of `90AF`) and on every later command failing
/// under a wrong Ke/Km. Do not "fix" this without hardware confirmation of
/// what those 8 bytes actually are.
pub fn parse_authenticate_host_pass2_response(resp: &[u8]) -> Result<[u8; 16], SamError> {
    if resp.len() != 26 {
        return Err(SamError::UnexpectedLength {
            got: resp.len(),
            expected: 26,
        });
    }
    let sw = u16::from_be_bytes([resp[24], resp[25]]);
    if sw != 0x90AF {
        return Err(SamError::Status(sw));
    }
    let mut out = [0u8; 16];
    out.copy_from_slice(&resp[8..24]);
    Ok(out)
}

/// Build pass 3 (final) of `SAM_AuthenticateHost`:
/// `80 A4 00 00 20 <EncKex(RndA || rotl(RndB, 2))[32]>` with `Le = 00`.
pub fn build_authenticate_host_pass3(ecipher: &[u8; 32]) -> Vec<u8> {
    let mut apdu = Vec::with_capacity(5 + 32 + 1);
    apdu.push(CLA);
    apdu.push(INS_AUTHENTICATE_HOST);
    apdu.push(0x00);
    apdu.push(0x00);
    apdu.push(32); // Lc
    apdu.extend_from_slice(ecipher);
    apdu.push(0x00); // Le
    apdu
}

/// Parse the SAM's response to pass 3: any leading data (ignored — see
/// HARDWARE-VERIFY below) followed by the final status word, which must be
/// `9000`. Any other status — including any `6xxx` error — is a hard
/// failure and the host session is not established.
///
/// HARDWARE-VERIFY: this does not decode/verify a returned
/// `EncKex(rotl(RndA, 2))` mutual-auth confirmation (the reference
/// implementation this is aligned to doesn't either). Practical security
/// still holds because every subsequent `SAM_ChangeKeyEntry`/etc. call is
/// itself encrypted+MAC'd under Ke/Km and will fail closed if the SAM did
/// not derive the same session keys — but a real mutual-auth check here
/// would catch a wrong-key session one step earlier.
pub fn parse_authenticate_host_pass3_response(resp: &[u8]) -> Result<(), SamError> {
    if resp.len() < 2 {
        return Err(SamError::UnexpectedLength {
            got: resp.len(),
            expected: 2,
        });
    }
    let sw = u16::from_be_bytes([resp[resp.len() - 2], resp[resp.len() - 1]]);
    if sw != 0x9000 {
        return Err(SamError::Status(sw));
    }
    Ok(())
}

// ---------------------------------------------------------------------
// SAM_LockUnlock (INS 10) — lock/unlock the SAM (state Unlock=0x00 is
// reversible; state SwitchAV2Mode=0x03 is NOT implemented here — that one is
// irreversible and out of scope for this project).
// ---------------------------------------------------------------------

pub const INS_LOCK_UNLOCK: u8 = 0x10;

/// `state` (P1) value to reversibly UNLOCK the SAM using a key entry's
/// current key. A factory-fresh MIFARE SAM AV3 ships LOCKED —
/// `SAM_AuthenticateHost` fails with `6985` until `SAM_LockUnlock`/Unlock is
/// run once, typically with the default master key (16×0x00, keyNo 0,
/// keyVer 0). See [`super::flows::lock_unlock_unlock`].
pub const LOCK_UNLOCK_STATE_UNLOCK: u8 = 0x00;

/// Build pass 1 of `SAM_LockUnlock`: `80 10 <state> 00 02 <keyNo> <keyVer> 00`.
///
/// HARDWARE-VERIFY: for `state = LOCK_UNLOCK_STATE_UNLOCK`, this exact byte
/// sequence is EMPIRICALLY CONFIRMED against our SAM AV3 hardware —
/// `80 10 00 00 02 00 00 00` returned `Rnd2[12] || 90 AF`
/// (`DC 28 4A D3 D2 22 2C 6D 83 78 B9 3A 90 AF`). Pass 2 below is transcribed
/// from `liblogicalaccess`'s `lockUnlock()` (commit 5f21b180, exercised
/// against real hardware) but is NOT yet confirmed end-to-end on this
/// project's own hardware — see [`super::flows::lock_unlock_unlock`].
pub fn build_lock_unlock_pass1(state: u8, key_no: u8, key_ver: u8) -> Vec<u8> {
    vec![
        CLA,
        INS_LOCK_UNLOCK,
        state,
        0x00,
        0x02,
        key_no,
        key_ver,
        0x00,
    ]
}

/// `SAM_LockUnlock` state P1 = `0x03` — "Activate SAM" (a.k.a. SwitchToAV2).
/// IRREVERSIBLE: activates a factory (ICS) SAM AV3 into MCS so GCS commands
/// (`SAM_AuthenticateHost`, `SAM_ChangeKeyEntry`) become available.
pub const LOCK_UNLOCK_STATE_ACTIVATE: u8 = 0x03;

/// Build pass 1 of `SAM_LockUnlock` in ACTIVATE mode:
/// `80 10 03 00 05 <keyNo> <keyVer> 00 00 00`.
///
/// Unlike the Unlock variant (Lc=02), the Activate/SwitchToAV2 state carries a
/// 3-byte `MaxChainBlocks` trailer (`00 00 00` = unlimited) after keyNo/keyVer,
/// so `Lc = 0x05`. Value per the liblogicalaccess `lockUnlock()` reference
/// (`ByteVector maxChainBlocks(3, 0x00)`) and NXP DS3235. Pass 2/3 are identical
/// to the Unlock flow (P1 there is always `0x00`); only the pass-2 MAC keys on
/// `state = 0x03` instead of `0x00`.
pub fn build_lock_unlock_activate_pass1(key_no: u8, key_ver: u8) -> Vec<u8> {
    vec![
        CLA,
        INS_LOCK_UNLOCK,
        LOCK_UNLOCK_STATE_ACTIVATE,
        0x00,
        0x05,
        key_no,
        key_ver,
        0x00, // MaxChainBlocks[0]
        0x00, // MaxChainBlocks[1]
        0x00, // MaxChainBlocks[2]
        0x00, // Le (case-4: pass 1 returns Rnd2[12] || 90AF)
    ]
}

/// Parse the SAM's response to `SAM_LockUnlock` pass 1: `Rnd2[12] || 90 AF`.
/// Byte-for-byte the same framing as
/// [`parse_authenticate_host_pass1_response`] (12-byte challenge + `90AF`
/// continuation status) — reused directly rather than re-implemented.
pub fn parse_lock_unlock_pass1_response(resp: &[u8]) -> Result<[u8; 12], SamError> {
    parse_authenticate_host_pass1_response(resp)
}

/// Build pass 2 of `SAM_LockUnlock`: `80 10 00 00 14 <macHost[8]> <rnd1[12]>`
/// with `Le = 00`. P1 is always `0x00` in pass 2 (the `state` was already
/// sent in pass 1); `macHost` is
/// [`super::crypto::host_auth_pass2_cmac`]`(masterKey, rnd2, state)` — the
/// exact same odd-byte-truncated-CMAC construction `SAM_AuthenticateHost`
/// pass 2 uses for its `cmac2`, just keyed on `state` instead of `hostMode`
/// (see [`super::flows::lock_unlock_unlock`]).
pub fn build_lock_unlock_pass2(mac_host: &[u8; 8], rnd1: &[u8; 12]) -> Vec<u8> {
    let mut apdu = Vec::with_capacity(5 + 20 + 1);
    apdu.push(CLA);
    apdu.push(INS_LOCK_UNLOCK);
    apdu.push(0x00);
    apdu.push(0x00);
    apdu.push(0x14); // Lc = 8 (macHost) + 12 (rnd1)
    apdu.extend_from_slice(mac_host);
    apdu.extend_from_slice(rnd1);
    apdu.push(0x00); // Le
    apdu
}

/// Parse the SAM's response to `SAM_LockUnlock` pass 2:
/// `macSam[8] || EncKex(RndB)[16] || 90 AF`.
///
/// Unlike [`parse_authenticate_host_pass2_response`] (which discards its
/// leading 8 bytes and keeps the middle 16 as `EncKex(RndB)`), here it's the
/// **leading** 8 bytes that are the SAM's own CMAC over
/// `rnd1 || state || 00 00 00`, which the caller must verify against its own
/// computation of the same CMAC (see [`super::flows::lock_unlock_unlock`]) —
/// fail-closed on any mismatch. `SAM_LockUnlock` is structurally the same
/// 3-pass mutual auth as `SAM_AuthenticateHost` (just `INS 0x10` and a
/// `state` byte instead of `hostMode`), so the middle 16 bytes here carry
/// the same `EncKex(RndB)` pass-3 continuation, not unused padding.
///
/// HARDWARE-VERIFY: transcribed from `liblogicalaccess`'s `lockUnlock()`
/// (commit 5f21b180, real-hardware-exercised) — see
/// [`super::flows::lock_unlock_unlock`].
pub fn parse_lock_unlock_pass2_response(resp: &[u8]) -> Result<([u8; 8], [u8; 16]), SamError> {
    if resp.len() != 26 {
        return Err(SamError::UnexpectedLength {
            got: resp.len(),
            expected: 26,
        });
    }
    let sw = u16::from_be_bytes([resp[24], resp[25]]);
    if sw != 0x90AF {
        return Err(SamError::Status(sw));
    }
    let mut mac_sam = [0u8; 8];
    mac_sam.copy_from_slice(&resp[0..8]);
    let mut enc_rnd_b = [0u8; 16];
    enc_rnd_b.copy_from_slice(&resp[8..24]);
    Ok((mac_sam, enc_rnd_b))
}

/// Build pass 3 (final) of `SAM_LockUnlock`:
/// `80 10 00 00 20 <EncKex(RndA || rotl(RndB, 2))[32]>` with `Le = 00`.
///
/// Byte-for-byte the same framing as [`build_authenticate_host_pass3`] except
/// for `INS` (`0x10` vs `0xA4`) — `state` is not resent here (it was only
/// sent in pass 1), matching pass 2's `P1 = 0x00` convention. See
/// [`super::flows::lock_unlock_unlock`].
pub fn build_lock_unlock_pass3(ecipher: &[u8; 32]) -> Vec<u8> {
    let mut apdu = Vec::with_capacity(5 + 32 + 1);
    apdu.push(CLA);
    apdu.push(INS_LOCK_UNLOCK);
    apdu.push(0x00);
    apdu.push(0x00);
    apdu.push(32); // Lc
    apdu.extend_from_slice(ecipher);
    apdu.push(0x00); // Le
    apdu
}

/// Parse the SAM's response to `SAM_LockUnlock` pass 3: any leading data
/// (ignored) followed by the final status word, which must be `9000`. Any
/// other status is a hard failure.
///
/// Byte-for-byte the same shape as
/// [`parse_authenticate_host_pass3_response`] — reused directly rather than
/// re-implemented. See that function's HARDWARE-VERIFY note (this does not
/// decode/verify the returned `EncKex(rotl(RndA, 2))` mutual-auth
/// confirmation either — the SAM having accepted `macHost`/reached `9000`
/// is what unlocks it; a real check here would only catch a wrong-key
/// session one step earlier).
pub fn parse_lock_unlock_pass3_response(resp: &[u8]) -> Result<(), SamError> {
    parse_authenticate_host_pass3_response(resp)
}

// ---------------------------------------------------------------------
// SAM_ChangeKeyEntry (INS C1)
// ---------------------------------------------------------------------

pub const INS_CHANGE_KEY_ENTRY: u8 = 0xC1;

/// `ProMas` bit flags (which fields of the key entry this command touches).
pub mod pro_mas {
    pub const VER_A: u8 = 0x80;
    pub const VER_B: u8 = 0x40;
    pub const VER_C: u8 = 0x20;
    pub const AID_KEYNO: u8 = 0x10;
    pub const CEK: u8 = 0x08;
    pub const KUC: u8 = 0x04;
    pub const SET: u8 = 0x02;
    pub const KEYVER_SEP: u8 = 0x01;
}

/// `SET` (2-byte, little-endian) key-entry flags.
pub mod set_flags {
    pub const ALLOW_DUMP_SESSION_KEY: u16 = 1 << 0;
    pub const KEEP_IV: u16 = 1 << 2;
    pub const AES128_TYPE: u16 = 1 << 5;
    pub const REQUIRE_AUTH: u16 = 1 << 7;
    pub const DISABLE_KEY_ENTRY: u16 = 1 << 9;
}

/// `ExtSET` (1 byte) key-entry flags. `KeyClass` occupies bits 0-2 and is
/// **not** a dense 0..=3 enum — `OfflineCrypto` is 4, skipping 3 — so store
/// the raw value rather than deriving it from a sequential discriminant.
pub mod ext_set_flags {
    pub const KEY_CLASS_HOST: u8 = 0;
    pub const KEY_CLASS_PICC: u8 = 1;
    pub const KEY_CLASS_OFFLINE_CHANGE: u8 = 2;
    pub const KEY_CLASS_OFFLINE_CRYPTO: u8 = 4;
    pub const ALLOW_DUMP_SECRET_KEY: u8 = 1 << 3;
    pub const RESTRICT_TO_DIVERSIFIED_USE: u8 = 1 << 4;
}

/// The 61-byte AES-128 SAM key-entry payload (the plaintext that gets
/// encrypted under `Ke` and MACed under `Km` for `SAM_ChangeKeyEntry` Full
/// mode).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AesKeyEntry {
    pub key_va: [u8; 16],
    pub key_vb: [u8; 16],
    pub key_vc: [u8; 16],
    pub df_aid: [u8; 3],
    pub df_key_no: u8,
    pub key_no_cek: u8,
    pub key_ver_cek: u8,
    pub ref_no_kuc: u8,
    /// `SET` flags — combine values from [`set_flags`].
    pub set: u16,
    pub ver_a: u8,
    pub ver_b: u8,
    pub ver_c: u8,
    /// `ExtSET` flags — combine values from [`ext_set_flags`].
    pub ext_set: u8,
}

impl AesKeyEntry {
    /// Exact wire length of an AES-128 SAM key entry.
    pub const LEN: usize = 61;

    /// Serialize to the exact 61-byte wire layout:
    /// `KeyVA[16] KeyVB[16] KeyVC[16] DF_AID[3] DF_KeyNo[1] KeyNoCEK[1]
    /// KeyVCEK[1] RefNoKUC[1] SET[2,LE] VerA[1] VerB[1] VerC[1] ExtSET[1]`.
    pub fn to_bytes(&self) -> [u8; Self::LEN] {
        let mut b = [0u8; Self::LEN];
        let mut o = 0usize;
        b[o..o + 16].copy_from_slice(&self.key_va);
        o += 16;
        b[o..o + 16].copy_from_slice(&self.key_vb);
        o += 16;
        b[o..o + 16].copy_from_slice(&self.key_vc);
        o += 16;
        b[o..o + 3].copy_from_slice(&self.df_aid);
        o += 3;
        b[o] = self.df_key_no;
        o += 1;
        b[o] = self.key_no_cek;
        o += 1;
        b[o] = self.key_ver_cek;
        o += 1;
        b[o] = self.ref_no_kuc;
        o += 1;
        b[o..o + 2].copy_from_slice(&self.set.to_le_bytes());
        o += 2;
        b[o] = self.ver_a;
        o += 1;
        b[o] = self.ver_b;
        o += 1;
        b[o] = self.ver_c;
        o += 1;
        b[o] = self.ext_set;
        o += 1;
        debug_assert_eq!(o, Self::LEN);
        b
    }
}

/// Build the fixed `CLA INS P1(entryNo) P2(ProMas)` header shared by every
/// `SAM_ChangeKeyEntry` protection mode.
pub fn build_change_key_entry_header(entry_no: u8, pro_mas: u8) -> [u8; 4] {
    [CLA, INS_CHANGE_KEY_ENTRY, entry_no, pro_mas]
}

/// Build the **plaintext** `SAM_ChangeKeyEntry` command exactly as
/// liblogicalaccess's `changeKeyEntry` assembles it before handing it to
/// `transmit`/`createfullProtectionCmd`:
/// `80 C1 <entryNo> <proMas> <Lc = 61> <61-byte key-entry data>` — **no Le**
/// (liblo's `cmd_vector` has no trailing Le; `getLcLe` then classifies it as
/// the "CLA INS P1 P2 LC DATA" shape).
///
/// This is the input to [`crypto::SecureMessaging::create_full_protection_cmd`],
/// which does the Full-protection wrapping (per-command enc IV, AES-CBC
/// encrypt under `Ke`, counter-in-MAC-input, chained CMAC under `Km`). Keep
/// this builder plaintext-only so the wrapping stays in one place, byte-exact
/// to the reference.
pub fn build_change_key_entry_plain(entry_no: u8, pro_mas: u8, entry: &AesKeyEntry) -> Vec<u8> {
    let raw = entry.to_bytes();
    let mut cmd = Vec::with_capacity(5 + raw.len());
    cmd.extend_from_slice(&build_change_key_entry_header(entry_no, pro_mas));
    cmd.push(raw.len() as u8); // Lc = 61 (0x3D)
    cmd.extend_from_slice(&raw);
    cmd
}

/// Compose a Full-mode `SAM_ChangeKeyEntry` command directly from an
/// [`AesKeyEntry`] and the host session keys: builds the plaintext command
/// ([`build_change_key_entry_plain`]) and wraps it with a fresh
/// [`crypto::SecureMessaging`] session (seeded exactly like the post-handshake
/// state — `cmd_ctr = 0`, all-zero IV chains). Byte-for-byte the
/// liblogicalaccess `createfullProtectionCmd` scheme: ISO/IEC 9797-1 method-2
/// padding, per-command enc IV via `generateEncIV`, the command counter folded
/// into the CMAC input (not the wire), and the odd-byte-truncated CMAC under
/// `Km`.
///
/// Convenience wrapper — [`ceremony::run_ceremony_on_sam`] drives the same
/// steps explicitly (build plaintext, `SecureMessaging::new`, wrap) so it can
/// keep the session for its result; both produce identical bytes.
///
/// HARDWARE-VERIFY: the post-LockUnlock SM seed (all-zero `last_mac_iv` /
/// `last_session_iv`, `cmd_ctr = 0`) is the assumed-correct starting state —
/// see [`crypto::SecureMessaging`]. Confirm on the bench with `SAM_APDU_DEBUG`.
pub fn build_change_key_entry_full_from_entry(
    entry_no: u8,
    pro_mas: u8,
    ke: &[u8; 16],
    km: &[u8; 16],
    entry: &AesKeyEntry,
) -> Result<Vec<u8>, SamError> {
    let plain = build_change_key_entry_plain(entry_no, pro_mas, entry);
    let mut sm = crypto::SecureMessaging::new(*ke, *km);
    sm.create_full_protection_cmd(&plain)
}

/// Build the encrypted-and-MAC'd key-entry payload (`encke`) for an
/// **offline** `SAM_ChangeKeyEntry` (the `KeyClass = OfflineChange` re-key
/// path — no host session; see [`crypto::generate_offline_session_key`] and
/// [`build_change_key_entry_offline`]).
///
/// **Construction (HARDWARE-VERIFY — this is the module's single biggest
/// offline-path assumption).** liblogicalaccess's `changeKeyEntryOffline`
/// (`~/.zeiterfassung-ops/sam-refs/samav2iso7816commands.cpp` lines 720-742)
/// takes `encke` as an already-built parameter and does **not** show how it
/// is assembled — that lives in the higher-level `SAMAV2Commands` template we
/// do not have. What we DO have is that `generateOfflineSessionKey`
/// deliberately populates the very same `d_sessionKey`/`d_macSessionKey`
/// slots that `createfullProtectionCmd` (the online Full-protection wrapper,
/// reference lines 334-415) consumes. So the most reference-faithful `encke`
/// is exactly that byte-for-byte wrapper driven by the **offline** `(Ke, Km)`
/// instead of a host session:
///
/// 1. Build the plaintext command `80 C1 <entryNo> <proMas> <Lc=61> <61B>`
///    ([`build_change_key_entry_plain`]).
/// 2. Wrap it with a fresh [`crypto::SecureMessaging`] seeded on `(Ke, Km)`
///    (`cmd_ctr = 0`, all-zero IV chains) — ISO/IEC 9797-1 method-2 padding
///    (61 -> 64), per-command enc IV via `generateEncIV`, AES-CBC encrypt
///    under `Ke`, the counter folded into the CMAC input, odd-byte-truncated
///    CMAC under `Km`.
/// 3. `encke = <encData[64]> || <MAC[8]>` — i.e. the wrapped wire frame with
///    its 5-byte `CLA INS P1 P2 Lc` header and trailing transport `Le`
///    stripped (those are re-framed by [`build_change_key_entry_offline`]
///    with the change counter).
///
/// Open questions this leaves for the bench (all revealed by the `SAM_OFF` /
/// `SAM_APDU_DEBUG` traces on a single run — see
/// [`super::ceremony::run_offline_ceremony_on_sam`]):
/// - whether the MAC the SAM recomputes covers the same online-style header
///   (`CLA INS entryNo proMas Lc'` + counter) that step 2 MACs over, or the
///   *offline* framing (with the 2-byte change counter and the offline `Lc`);
/// - whether the folded counter should be `0` (as here — the fresh-session
///   default, since the change counter is already bound into `Ke`/`Km` via
///   the SV) or the change counter itself;
/// - whether the change counter belongs inside `encke`'s MAC at all.
///
/// If the SAM rejects this with a MAC/`6xxx` error, the trace shows the exact
/// bytes so the alternative can be tried without re-guessing blind.
pub fn build_encke_offline(
    entry_no: u8,
    pro_mas: u8,
    ke: &[u8; 16],
    km: &[u8; 16],
    entry: &AesKeyEntry,
) -> Result<Vec<u8>, SamError> {
    let plain = build_change_key_entry_plain(entry_no, pro_mas, entry);
    let mut sm = crypto::SecureMessaging::new(*ke, *km);
    let wrapped = sm.create_full_protection_cmd(&plain)?;
    // wrapped = [CLA INS P1 P2 Lc'] [encData(64)] [MAC(8)]  (no trailing Le).
    // encke = encData || MAC — strip only the 5-byte header.
    const HDR: usize = 5;
    if wrapped.len() <= HDR {
        return Err(SamError::Crypto(format!(
            "build_encke_offline: wrapped command too short ({} bytes)",
            wrapped.len()
        )));
    }
    Ok(wrapped[HDR..].to_vec())
}

/// Build the **plaintext** offline `SAM_ChangeKeyEntry` command, byte-for-byte
/// per liblogicalaccess `changeKeyEntryOffline`
/// (`~/.zeiterfassung-ops/sam-refs/samav2iso7816commands.cpp` lines 720-742):
///
/// `80 C1 <entryNo> <proMas> <Lc = 2 + encke.len()> <changecnt_hi>
/// <changecnt_lo> <encke...>` — **no Le** (the reference's `cmd_vector` has
/// no trailing Le, and the command is transmitted *plain*: the offline path
/// has no host session, so liblo's `transmit` takes its
/// `d_sessionKey.size()==0` branch and sends the bytes as-is — reference
/// lines 596-597).
///
/// `changecnt` is big-endian (reference lines 729-730), and **must** be the
/// same value passed to [`crypto::generate_offline_session_key`] to derive
/// `(Ke, Km)` — the SAM re-derives the session from its stored current key +
/// this counter, so a mismatch fails the change closed.
pub fn build_change_key_entry_offline(
    entry_no: u8,
    pro_mas: u8,
    changecnt: u16,
    encke: &[u8],
) -> Vec<u8> {
    let lc = 2 + encke.len();
    assert!(
        lc <= 255,
        "offline ChangeKeyEntry Lc ({lc}) exceeds a short APDU's single-byte Lc"
    );
    let mut cmd = Vec::with_capacity(5 + lc);
    cmd.extend_from_slice(&build_change_key_entry_header(entry_no, pro_mas));
    cmd.push(lc as u8);
    cmd.push((changecnt >> 8) as u8);
    cmd.push((changecnt & 0xff) as u8);
    cmd.extend_from_slice(encke);
    cmd
}

// ---------------------------------------------------------------------
// SAM_ChangeKeyPICC (INS C4) — diversify + write a key onto the card.
//
// SUPERSEDED 2026-07-15 (bead 4ty rework, see `personalize`'s module-level
// "Incident 2026-07-15" doc comment): this "live SAM relay" model
// (`SAM_AuthenticatePICC` establishes a SAM-tracked PICC session, then
// `SAM_ChangeKeyPICC` diversifies+writes through it) has been replaced by
// the OFFLINE model (`SAM_DumpSecretKey` below hands the raw diversified key
// to the host, which does the DESFire `AuthenticateEV2First`+`ChangeKey`
// crypto itself — see `crypto::DesfireEv2Session`). No active flow in this
// codebase calls `build_change_key_picc`/`build_picc_change_key_from_sam_payload`
// any more; kept (not deleted) for reference and because their tests still
// document a plausible wire format, per this project's "mark superseded
// code clearly rather than deleting it" convention.
// ---------------------------------------------------------------------

pub const INS_CHANGE_KEY_PICC: u8 = 0xC4;
pub const CHANGE_KEY_PICC_APP_KEY_DIV: u8 = 0x22;
pub const CHANGE_KEY_PICC_APP_MASTER_DIV: u8 = 0x23;

/// Build `SAM_ChangeKeyPICC`: `80 C4 <p1> 00 <Lc> <divInput...>`.
///
/// HARDWARE-VERIFY: the trailing `Le = 00` byte is assumed for consistency
/// with the other case-4 APDUs in this module; it is not explicitly spelled
/// out in the source spec for this command.
///
/// SUPERSEDED — see the section-level doc comment above; no active flow
/// calls this any more.
#[allow(dead_code)]
pub fn build_change_key_picc(p1: u8, div_input: &[u8]) -> Vec<u8> {
    assert!(div_input.len() <= 255);
    let mut apdu = Vec::with_capacity(5 + div_input.len() + 1);
    apdu.push(CLA);
    apdu.push(INS_CHANGE_KEY_PICC);
    apdu.push(p1);
    apdu.push(0x00);
    apdu.push(div_input.len() as u8);
    apdu.extend_from_slice(div_input);
    apdu.push(0x00); // Le — HARDWARE-VERIFY, see doc comment above.
    apdu
}

// ---------------------------------------------------------------------
// SAM_DumpSecretKey (INS D6) — bead 4ty rework 2026-07-15: hand the raw
// (optionally diversified) key material for a SAM key entry to the host, so
// the host can do the DESFire `AuthenticateEV2First`/`ChangeKey` crypto
// itself instead of relaying through `SAM_AuthenticatePICC`/
// `SAM_ChangeKeyPICC` — see the `personalize` module-level "Incident
// 2026-07-15" doc comment and `crypto::DesfireEv2Session`.
// ---------------------------------------------------------------------

pub const INS_DUMP_SECRET_KEY: u8 = 0xD6;

/// `P1` bit meaning "a `DivInput` is present" for `SAM_DumpSecretKey`, per
/// liblogicalaccess `dumpSecretKey`'s `if (divInpu.size()) p1 |= 0x02;`
/// (`~/.zeiterfassung-ops/sam-refs/samav2iso7816commands.cpp` lines 789-811).
pub const DUMP_SECRET_KEY_P1_DIVERSIFIED: u8 = 0x02;

/// Build the PLAINTEXT `SAM_DumpSecretKey` command, ready for
/// [`crypto::SecureMessaging::full_protection_round_trip`] — a Full-mode
/// command like `SAM_ChangeKeyEntry`, not a bare S-mode one, per the
/// reference (`dumpSecretKey` calls `transmit(cmd_vector)` with its default
/// `first=true,last=true`, which wraps via `createfullProtectionCmd` whenever
/// a host session — `d_sessionKey` — is active):
///
/// `80 D6 <p1> 00 <Lc = 2 + divInput.len()> <keyNo> <keyVersion>
/// <divInput...> <Le = 00>`
///
/// `p1` gets [`DUMP_SECRET_KEY_P1_DIVERSIFIED`] OR'd in automatically
/// whenever `div_input` is non-empty. This project's only caller always
/// passes a non-empty `div_input` (per-fob AN10922 diversification, see
/// [`crypto::diversification_input`]).
///
/// **Trailing `Le = 0x00` — byte-exact to the reference (ISO case 4).**
/// `SAM_DumpSecretKey` is a *data-returning* command: it hands the 16-byte
/// diversified key back to the host. The reference's plaintext `cmd_vector`
/// therefore ends in a literal `0x00` Le byte (`unsigned char cmd[] = {..,
/// keyno, keyversion, 0x00}`, `samav2iso7816commands.cpp` lines 789-811),
/// and `createfullProtectionCmd`'s "LC DATA LE" branch carries that Le —
/// unencrypted — into both the CMAC input (`protectedCmd`) and the final
/// wire command *after* the 8-byte MAC (`finalFullProtectedCmd`).
///
/// **Why this was corrected (bead eyc / zeiterfassung-eyc, 2026-07-15):** an
/// earlier revision omitted the Le on the (mistaken) precedent that a trailing
/// Le caused `6A84` for `SAM_ChangeKeyEntry` (bead 59m). But `ChangeKeyEntry`
/// is a *case-3* command (no response data) where an Le is genuinely spurious;
/// `DumpSecretKey` is *case 4* and the Le is what tells the SAM to emit the
/// key. Sending it Le-less produced exactly the empirically-observed failure:
/// the SAM accepted the (validly-MAC'd) command with `9000` but returned a
/// data-less ~9-byte Full-protection response (`<1 pad byte><MAC8>` + SW) —
/// no key. Restoring the Le (and teaching
/// [`crypto::SecureMessaging::create_full_protection_cmd`] the "LC DATA LE"
/// shape) is the reference-faithful fix. CONFIRMED on hardware: the wrapped
/// request returns the enciphered key (`<32-byte E(key||pad)><MAC8><9000>`).
pub fn build_dump_secret_key_plain(key_no: u8, key_ver: u8, div_input: &[u8]) -> Vec<u8> {
    assert!(
        div_input.len() <= 253,
        "DivInput too large for a short APDU (Lc = 2 + div_input must fit one byte)"
    );
    let p1 = if div_input.is_empty() {
        0x00
    } else {
        DUMP_SECRET_KEY_P1_DIVERSIFIED
    };
    let lc = 2 + div_input.len();
    let mut cmd = Vec::with_capacity(5 + lc + 1);
    cmd.push(CLA);
    cmd.push(INS_DUMP_SECRET_KEY);
    cmd.push(p1);
    cmd.push(0x00);
    cmd.push(lc as u8);
    cmd.push(key_no);
    cmd.push(key_ver);
    cmd.extend_from_slice(div_input);
    cmd.push(0x00); // Le — case-4, tells the SAM to return the diversified key
    cmd
}

// ---------------------------------------------------------------------
// SAM_AuthenticatePICC (INS 0A) — EV2/EV1 mutual authentication via the SAM.
//
// SUPERSEDED for BOTH callers as of the bead bi0 rework (2026-07-15): the
// kiosk's daily punch authentication (`punch::authenticate_and_read_token_id`)
// abandoned this SAM-mediated relay for the same offline (SAM_DumpSecretKey +
// software EV2) model personalization (bead 4ty) moved to first — the SAM
// returned 6A84 on this relay for a real diversified PICC-class key entry on
// our AV3-mode SAM. Neither `authenticate_picc_ev2` nor
// `authenticate_picc_ev2_diversified` (below) is used by any active flow any
// more; both are kept (not deleted) as general-purpose SAM-relayed EV2
// primitives — see their doc comments in `flows.rs`.
// ---------------------------------------------------------------------

pub const INS_AUTHENTICATE_PICC: u8 = 0x0A;

/// `P1` values for `SAM_AuthenticatePICC`.
pub const AUTH_PICC_P1_EV2_FIRST: u8 = 0x80;
/// HARDWARE-VERIFY: EV2 NonFirst SAM P1 value per spec; the corresponding
/// card-side opcode ([`PICC_CMD_AUTHENTICATE_EV2_NON_FIRST`]) is
/// unconfirmed, and this module does not wire a NonFirst flow yet.
pub const AUTH_PICC_P1_EV2_NON_FIRST: u8 = 0xC0;
pub const AUTH_PICC_P1_EV1_WITH_DIV: u8 = 0x03;
pub const AUTH_PICC_P1_EV1_NO_DIV: u8 = 0x00;

/// `AuthMode` byte inside the pass-1 data field (fixed at `0x00` per spec).
pub const AUTH_MODE_DEFAULT: u8 = 0x00;

/// Build pass 1 of `SAM_AuthenticatePICC`:
/// `80 0A <p1> 00 <Lc=19> <keyEntryNo> <keyVer> <authMode=00> <Ek(RndB)[16]>`
/// with `Le = 00`.
pub fn build_authenticate_picc_pass1(
    p1: u8,
    key_entry_no: u8,
    key_ver: u8,
    ek_rnd_b: &[u8; 16],
) -> Vec<u8> {
    let mut apdu = Vec::with_capacity(5 + 19 + 1);
    apdu.push(CLA);
    apdu.push(INS_AUTHENTICATE_PICC);
    apdu.push(p1);
    apdu.push(0x00);
    apdu.push(19); // Lc = 1 (keyEntryNo) + 1 (keyVer) + 1 (authMode) + 16 (Ek(RndB))
    apdu.push(key_entry_no);
    apdu.push(key_ver);
    apdu.push(AUTH_MODE_DEFAULT);
    apdu.extend_from_slice(ek_rnd_b);
    // No trailing Le: SAM_AuthenticatePICC is a case-3 GCS key command. A
    // stray Le makes the SAM return 6A84 (proven on HW 2026-07-15, bead eyc —
    // the same fix the runbook §7 documents for ChangeKeyEntry).
    apdu
}

/// Parse the SAM's response to pass 1: `PCDChalResp[32] || 90 AF`. `90AF`
/// means "continue" (more data expected in pass 2); any other status word
/// is a hard failure — including any `6xxx` SAM error status.
pub fn parse_authenticate_picc_pass1_response(resp: &[u8]) -> Result<[u8; 32], SamError> {
    if resp.len() != 34 {
        return Err(SamError::UnexpectedLength {
            got: resp.len(),
            expected: 34,
        });
    }
    let sw = u16::from_be_bytes([resp[32], resp[33]]);
    if sw != 0x90AF {
        return Err(SamError::Status(sw));
    }
    let mut out = [0u8; 32];
    out.copy_from_slice(&resp[..32]);
    Ok(out)
}

/// Build pass 2 of `SAM_AuthenticatePICC`: `80 0A 00 00 <Lc=20> <PDResp[32]>`
/// with `Le = 00`.
///
/// HARDWARE-VERIFY: the source spec writes this step as
/// `<Lc> <PDResp> 00` without labeling the trailing byte; this builder
/// interprets it as a standard ISO 7816 case-4 `Le = 00` trailer (so
/// `Lc = 32`, the length of `PDResp` alone), not as a 33rd data byte. If
/// hardware testing shows the SAM expects `Lc = 33` with a data byte of
/// `0x00` appended after `PDResp` instead, this needs correcting.
pub fn build_authenticate_picc_pass2(pd_resp: &[u8; 32]) -> Vec<u8> {
    let mut apdu = Vec::with_capacity(5 + 32 + 1);
    apdu.push(CLA);
    apdu.push(INS_AUTHENTICATE_PICC);
    apdu.push(0x00);
    apdu.push(0x00);
    apdu.push(32); // Lc
    apdu.extend_from_slice(pd_resp);
    apdu.push(0x00); // Le
    apdu
}

/// `P1` value for a *diversified* EV2-First `SAM_AuthenticatePICC` pass 1
/// (bead bi0 — the kiosk's daily punch authenticates against each fob's
/// per-card diversified app key, unlike 3pn/4ty which only ever authenticate
/// against the non-diversified factory-default key).
///
/// HARDWARE-VERIFY (significant, unconfirmed): this project's only existing
/// diversified/non-diversified P1 distinction is for the EV1 family
/// (`AUTH_PICC_P1_EV1_WITH_DIV = 0x03` vs `AUTH_PICC_P1_EV1_NO_DIV = 0x00`),
/// which sets bits 0-1 as a "DivMode" field. This constant extrapolates that
/// same 2-bit pattern onto EV2-First (`AUTH_PICC_P1_EV2_FIRST | 0x03`); it is
/// **not** confirmed against the DS3235 spec text or real SAM AV3 hardware —
/// bi0 is this project's first caller needing diversified-key EV2
/// authentication. Verify on the bench before trusting this in production;
/// if wrong, the SAM will simply reject the command with a `6xxx` status
/// (fail-closed — no security exposure from getting this byte wrong, just an
/// operational "every tap fails" symptom until corrected).
pub const AUTH_PICC_P1_EV2_FIRST_DIV: u8 = AUTH_PICC_P1_EV2_FIRST | AUTH_PICC_P1_EV1_WITH_DIV;

/// Build pass 1 of a *diversified* `SAM_AuthenticatePICC` EV2-First:
/// `80 0A <AUTH_PICC_P1_EV2_FIRST_DIV> 00 <Lc> <keyEntryNo> <keyVer>
/// <authMode=00> <Ek(RndB)[16]> <DivInput...>` with `Le = 00`. Identical to
/// [`build_authenticate_picc_pass1`] except for the P1 value and the
/// trailing `DivInput` bytes appended to the data field.
///
/// HARDWARE-VERIFY: besides the P1 value (see
/// [`AUTH_PICC_P1_EV2_FIRST_DIV`]), appending `DivInput` after `Ek(RndB)` in
/// the pass-1 data field is an extrapolation from how `SAM_ChangeKeyPICC`
/// takes `DivInput` as its payload (see [`build_change_key_picc`]) — not
/// confirmed against real hardware or spec text.
pub fn build_authenticate_picc_pass1_diversified(
    key_entry_no: u8,
    key_ver: u8,
    ek_rnd_b: &[u8; 16],
    div_input: &[u8],
) -> Vec<u8> {
    assert!(
        div_input.len() <= 255 - 19,
        "DivInput too large for a short APDU"
    );
    let lc = 19 + div_input.len();
    let mut apdu = Vec::with_capacity(5 + lc + 1);
    apdu.push(CLA);
    apdu.push(INS_AUTHENTICATE_PICC);
    apdu.push(AUTH_PICC_P1_EV2_FIRST_DIV);
    apdu.push(0x00);
    apdu.push(lc as u8);
    apdu.push(key_entry_no);
    apdu.push(key_ver);
    apdu.push(AUTH_MODE_DEFAULT);
    apdu.extend_from_slice(ek_rnd_b);
    apdu.extend_from_slice(div_input);
    // No trailing Le — see build_authenticate_picc_pass1.
    apdu
}

/// Parse the SAM's response to pass 2: `caps || 90 00`. Any status other
/// than `9000` — including any `6xxx` error — is a hard failure.
pub fn parse_authenticate_picc_pass2_response(resp: &[u8]) -> Result<Vec<u8>, SamError> {
    if resp.len() < 2 {
        return Err(SamError::UnexpectedLength {
            got: resp.len(),
            expected: 2,
        });
    }
    let (caps, sw_bytes) = resp.split_at(resp.len() - 2);
    let sw = u16::from_be_bytes([sw_bytes[0], sw_bytes[1]]);
    if sw != 0x9000 {
        return Err(SamError::Status(sw));
    }
    Ok(caps.to_vec())
}

// ---------------------------------------------------------------------
// DESFire-native (card-side) frames for the AuthenticatePICC handshake.
//
// These are NOT SAM APDUs (no CLA/INS/P1/P2/Lc/Le framing) — they are the
// raw native command bytes exchanged with the card over ISO14443-4.
// ---------------------------------------------------------------------

pub const PICC_CMD_AUTHENTICATE_EV2_FIRST: u8 = 0x71;
/// HARDWARE-VERIFY: AuthenticateEV2NonFirst native opcode. Commonly
/// documented elsewhere as `0x77` for the DESFire EV2/EV3 family, but not
/// confirmed against this project's real card+SAM pairing. The NonFirst
/// flow itself is out of scope for this step.
pub const PICC_CMD_AUTHENTICATE_EV2_NON_FIRST: u8 = 0x77;
pub const PICC_CMD_AUTHENTICATE_AES_EV1: u8 = 0xAA;

/// Native status/continuation byte meaning "additional frame follows" —
/// used both as the command prefix the host sends on a continuation and as
/// the status byte the card returns while more data is expected.
pub const PICC_AF: u8 = 0xAF;
/// Native status byte meaning "operation complete, success".
pub const PICC_OK: u8 = 0x00;

/// HARDWARE-VERIFY: `AuthenticateEV2First` (and its continuation frames) are
/// sent **natively** over ISO14443-4 — i.e. NOT wrapped in an ISO 7816-4
/// case-2/3/4 APDU envelope the way `SAM_AuthenticatePICC` is. Whether the
/// PC/SC reader firmware itself expects `Card::transmit` to be called with
/// exactly these raw bytes (as assumed here), or requires an extra
/// envelope/escape for native contactless frames, is unconfirmed until
/// tested against the real ACR1252U + card.
pub fn build_picc_authenticate_ev2_first(key_no: u8) -> Vec<u8> {
    vec![PICC_CMD_AUTHENTICATE_EV2_FIRST, key_no, 0x00]
}

/// EV1 native `AuthenticateAES` command (`0xAA <KeyNo> 00`). Not used by
/// the EV2 flow in this module; provided for completeness per the spec's
/// command table.
pub fn build_picc_authenticate_ev1(key_no: u8) -> Vec<u8> {
    vec![PICC_CMD_AUTHENTICATE_AES_EV1, key_no, 0x00]
}

/// Build a native continuation frame: `AF || data`.
pub fn build_picc_continue(data: &[u8]) -> Vec<u8> {
    let mut frame = Vec::with_capacity(1 + data.len());
    frame.push(PICC_AF);
    frame.extend_from_slice(data);
    frame
}

/// Parse step 2's native PICC response `<status> || Ek(RndB)[16]` — a
/// single AES block, distinct from the 32-byte frames used later in the
/// handshake (`RndB` itself is 16 bytes; steps 4/6 exchange two-block, 32-
/// byte payloads). Checks the leading status byte against `expected_status`
/// ([`PICC_AF`] is expected here). Any other status byte — including any
/// DESFire native error code — is a hard failure.
pub fn parse_picc_frame_ek_rnd_b(resp: &[u8], expected_status: u8) -> Result<[u8; 16], SamError> {
    if resp.len() != 17 {
        return Err(SamError::UnexpectedLength {
            got: resp.len(),
            expected: 17,
        });
    }
    if resp[0] != expected_status {
        return Err(SamError::PiccStatus(resp[0]));
    }
    let mut out = [0u8; 16];
    out.copy_from_slice(&resp[1..]);
    Ok(out)
}

/// Parse step 6's native 33-byte PICC response `<status> || PDResp[32]`,
/// checking the leading status byte against `expected_status` ([`PICC_OK`]
/// is expected here). Any other status byte — including any DESFire native
/// error code — is a hard failure.
pub fn parse_picc_frame_pd_resp(resp: &[u8], expected_status: u8) -> Result<[u8; 32], SamError> {
    if resp.len() != 33 {
        return Err(SamError::UnexpectedLength {
            got: resp.len(),
            expected: 33,
        });
    }
    if resp[0] != expected_status {
        return Err(SamError::PiccStatus(resp[0]));
    }
    let mut out = [0u8; 32];
    out.copy_from_slice(&resp[1..]);
    Ok(out)
}

// ---------------------------------------------------------------------
// SAM_Apply_SM (INS AE) / SAM_Remove_SM (INS AD) — secure-messaging wrap
// for DESFire commands sent through the SAM (e.g. reading the token-ID).
// ---------------------------------------------------------------------

pub const INS_APPLY_SM: u8 = 0xAE;
pub const INS_REMOVE_SM: u8 = 0xAD;

pub const COMM_MODE_PLAIN: u8 = 0x00;
pub const COMM_MODE_MAC: u8 = 0x10;
pub const COMM_MODE_FULL: u8 = 0x30;

/// DESFire `ReadData` native command byte — used for the token-id read
/// (bead bi0, `punch::authenticate_and_read_token_id`), either wrapped via
/// `Apply_SM`/`Remove_SM` (`flows::read_token_id`, superseded 2026-07-15) or
/// sent directly to the card under a software EV2 session
/// (`flows::read_token_id_software`, the active flow as of the same rework).
pub const DESFIRE_CMD_READ_DATA: u8 = 0xBD;

/// Build a `ReadData` command:
/// `BD <FileNo> <Offset[3], LSB-first> <Length[3], LSB-first>`.
///
/// Mirrors [`build_picc_write_data`]'s framing exactly (same field widths and
/// byte order), minus the trailing data payload — read has no bytes to send,
/// only to receive back. The active flow
/// ([`super::flows::read_token_id_software`]) appends an EV2 CMACt directly
/// to this header and sends it straight to the card; the superseded
/// `flows::read_token_id` instead wrapped this via `Apply_SM`/`Remove_SM`
/// (CommMode.Full for the token-id file, per 4ty's
/// `PersonalizeConfig::file_comm_settings`).
///
/// HARDWARE-VERIFY: field widths/order follow the commonly-published
/// DESFire layout, same caveat as `build_picc_write_data`; unconfirmed
/// against this project's cards.
pub fn build_picc_read_data(file_no: u8, offset: u32, length: u32) -> Vec<u8> {
    assert!(offset <= 0x00FF_FFFF, "Offset must fit in 3 bytes");
    assert!(length <= 0x00FF_FFFF, "Length must fit in 3 bytes");
    let mut cmd = Vec::with_capacity(7);
    cmd.push(DESFIRE_CMD_READ_DATA);
    cmd.push(file_no);
    cmd.extend_from_slice(&offset.to_le_bytes()[..3]);
    cmd.extend_from_slice(&length.to_le_bytes()[..3]);
    cmd
}

/// Build `SAM_Apply_SM`: `80 AE 00 <commMode> <Lc> <desfireCmd...>` with
/// `Le = 00`. Call this even in [`COMM_MODE_PLAIN`] to keep the SAM's R/W
/// counters synchronized with the card.
///
/// HARDWARE-VERIFY: `P1` is assumed `0x00` — only `P2 = CommMode` is
/// documented in the source spec.
pub fn build_apply_sm(comm_mode: u8, desfire_cmd: &[u8]) -> Vec<u8> {
    assert!(desfire_cmd.len() <= 255);
    let mut apdu = Vec::with_capacity(5 + desfire_cmd.len() + 1);
    apdu.push(CLA);
    apdu.push(INS_APPLY_SM);
    apdu.push(0x00); // P1 — HARDWARE-VERIFY, see doc comment above.
    apdu.push(comm_mode);
    apdu.push(desfire_cmd.len() as u8);
    apdu.extend_from_slice(desfire_cmd);
    apdu.push(0x00); // Le
    apdu
}

/// Build `SAM_Remove_SM`: `80 AD 00 30 <Lc> <cardResponse incl. status byte>`
/// with `Le = 00`. `P2` is always [`COMM_MODE_FULL`] — Remove_SM
/// verifies+decrypts the card's response.
pub fn build_remove_sm(card_response: &[u8]) -> Vec<u8> {
    assert!(card_response.len() <= 255);
    let mut apdu = Vec::with_capacity(5 + card_response.len() + 1);
    apdu.push(CLA);
    apdu.push(INS_REMOVE_SM);
    apdu.push(0x00); // P1 — HARDWARE-VERIFY, see build_apply_sm.
    apdu.push(COMM_MODE_FULL);
    apdu.push(card_response.len() as u8);
    apdu.extend_from_slice(card_response);
    apdu.push(0x00); // Le
    apdu
}

/// Parse a generic SAM response with a `9000` success status word, returning
/// the leading data bytes. Any other status word — including any `6xxx`
/// error — is a hard failure. Used by `Apply_SM`/`Remove_SM` callers.
pub fn parse_status_ok(resp: &[u8]) -> Result<&[u8], SamError> {
    if resp.len() < 2 {
        return Err(SamError::UnexpectedLength {
            got: resp.len(),
            expected: 2,
        });
    }
    let (data, sw_bytes) = resp.split_at(resp.len() - 2);
    let sw = u16::from_be_bytes([sw_bytes[0], sw_bytes[1]]);
    if sw != 0x9000 {
        return Err(SamError::Status(sw));
    }
    Ok(data)
}

// ---------------------------------------------------------------------
// DESFire-native (card-side) frames for fob personalization (bead 4ty):
// SelectApplication, CreateApplication, CreateStdDataFile, WriteData, and
// the native ChangeKey wrapper used to relay `SAM_ChangeKeyPICC`'s output
// to the card. Like the AuthenticatePICC frames above, these are raw
// native DESFire command bytes — sent either directly to the PICC (no SM
// active) or wrapped via `Apply_SM`/`Remove_SM` once framing is needed (see
// `flows::send_via_sm`, used by `personalize::personalize_fob`).
//
// HARDWARE-VERIFY (module-wide): every byte order / field-width choice
// below follows the commonly-published DESFire EV1/EV2 native command
// layout (LSB-first multi-byte fields, no ISO/7816 file-ID/DF-name extras)
// — none of it has been exercised against this project's actual DESFire
// EV2 cards yet. See the per-function notes for the specific open
// questions.
// ---------------------------------------------------------------------

pub const DESFIRE_CMD_SELECT_APPLICATION: u8 = 0x5A;
pub const DESFIRE_CMD_CREATE_APPLICATION: u8 = 0xCA;
pub const DESFIRE_CMD_CREATE_STD_DATA_FILE: u8 = 0xCD;
pub const DESFIRE_CMD_WRITE_DATA: u8 = 0x3D;
/// Native `ChangeKey` command byte.
///
/// HARDWARE-VERIFY: this shares its numeric value with
/// [`INS_CHANGE_KEY_PICC`] (`0xC4`), but the two are unrelated — that one is
/// a *SAM* APDU instruction byte sent with `CLA=0x80` to the SAM slot; this
/// one is the *native* DESFire card command byte sent only to the PICC. The
/// shared value is a (published) coincidence, not a relationship between
/// the two commands.
pub const DESFIRE_CMD_CHANGE_KEY: u8 = 0xC4;

/// Build the native `SelectApplication` command: `5A <AID[3], LSB-first>`.
pub fn build_picc_select_application(aid: &[u8; 3]) -> Vec<u8> {
    let mut cmd = Vec::with_capacity(4);
    cmd.push(DESFIRE_CMD_SELECT_APPLICATION);
    cmd.extend_from_slice(aid);
    cmd
}

/// `KeySettings1` bit flags for `CreateApplication` — who may change keys,
/// and whether the app master key itself is changeable/listable without
/// prior authentication. Bits 4-7 (not modelled as named constants) carry
/// `ChangeKeyAccessRights` — which key number may change other keys
/// (`0xE` = "only the same key may change itself", `0xF` = "only the app
/// master key may change any key").
///
/// HARDWARE-VERIFY: these are the commonly-published DESFire EV1/EV2
/// key-settings semantics, not confirmed against this project's own cards.
pub mod key_settings1 {
    pub const ALLOW_CHANGE_MASTER_KEY: u8 = 1 << 0;
    pub const FREE_DIRECTORY_LIST_WITHOUT_MASTER_KEY: u8 = 1 << 1;
    pub const FREE_CREATE_DELETE_WITHOUT_MASTER_KEY: u8 = 1 << 2;
    pub const CONFIGURATION_CHANGEABLE: u8 = 1 << 3;
}

/// `KeySettings2` bits for `CreateApplication`. Per the published DESFire
/// EV1/EV2/EV3 command set (NXP AN10787 / MF3ICD81 datasheet, Table
/// "KeySettings2"; mirrored in libfreefare's
/// `mifare_desfire_create_application`), the application's crypto method
/// lives in **bits 7-6**: `00` = DES/2K3DES, `0x40` = 3K3DES, `0x80` = AES.
/// **Bit 5 (`0x20`) is unrelated** — it is the "use 2-byte ISO/IEC 7816-4
/// File IDs" flag (`MDAPP_ISO_FILE_IDS` in libfreefare's naming), not part
/// of the crypto-method encoding at all.
///
/// FIXED 2026-07-15: this constant was previously `1 << 5` (`0x20`), which
/// set the ISO-FID flag instead of selecting AES — every app created by
/// this project's personalization flow was silently DES/2K3DES-typed, not
/// AES, which is why its default (all-zero) app key 0 could never
/// authenticate via `AuthenticateEV2First`/AES. Root-caused against live
/// hardware probes the same day (see `personalize::personalize_fob`'s
/// module-level doc comment for the full incident).
///
/// The low nibble (not modelled here) carries the number of keys in the
/// app; callers OR it in directly, e.g. `KEY_SETTINGS2_AES | 0x01` for a
/// 1-key app.
pub const KEY_SETTINGS2_AES: u8 = 1 << 7;

/// Build the native `CreateApplication` command:
/// `CA <AID[3]> <KeySettings1> <KeySettings2>` (no ISO file-ID / DF-name
/// extras — this project does not use DESFire's ISO 7816 file-system
/// mapping).
pub fn build_picc_create_application(
    aid: &[u8; 3],
    key_settings1: u8,
    key_settings2: u8,
) -> Vec<u8> {
    let mut cmd = Vec::with_capacity(6);
    cmd.push(DESFIRE_CMD_CREATE_APPLICATION);
    cmd.extend_from_slice(aid);
    cmd.push(key_settings1);
    cmd.push(key_settings2);
    cmd
}

/// `CommSettings` byte for `CreateStdDataFile` (and other file-creation
/// commands) — a distinct constant space from [`COMM_MODE_PLAIN`] etc. even
/// though the numeric values happen to match (both follow the same
/// published DESFire convention: `00`=plain, `01`=MACed, `03`=enciphered).
pub mod file_comm_settings {
    pub const PLAIN: u8 = 0x00;
    pub const MACED: u8 = 0x01;
    pub const ENCIPHERED: u8 = 0x03;
}

/// Build a `CreateStdDataFile` command:
/// `CD <FileNo> <CommSettings> <AccessRights[2], LSB-first> <FileSize[3], LSB-first>`.
///
/// HARDWARE-VERIFY: `FileSize` field width (3 bytes) and byte order
/// (LSB-first) follow the commonly-published DESFire layout; unconfirmed
/// against this project's cards. `access_rights` packs
/// Read/Write/ReadWrite/ChangeAccessRights key numbers per the standard
/// DESFire 4-nibble layout — this module does not further decompose it
/// (callers pass the raw `u16`).
pub fn build_picc_create_std_data_file(
    file_no: u8,
    comm_settings: u8,
    access_rights: u16,
    file_size: u32,
) -> Vec<u8> {
    assert!(file_size <= 0x00FF_FFFF, "FileSize must fit in 3 bytes");
    let mut cmd = Vec::with_capacity(8);
    cmd.push(DESFIRE_CMD_CREATE_STD_DATA_FILE);
    cmd.push(file_no);
    cmd.push(comm_settings);
    cmd.extend_from_slice(&access_rights.to_le_bytes());
    let size = file_size.to_le_bytes();
    cmd.extend_from_slice(&size[..3]);
    cmd
}

/// Build a `WriteData` command:
/// `3D <FileNo> <Offset[3], LSB-first> <Length[3], LSB-first> <Data...>`.
///
/// HARDWARE-VERIFY: field widths/order follow the commonly-published
/// DESFire layout; unconfirmed against this project's cards.
pub fn build_picc_write_data(file_no: u8, offset: u32, data: &[u8]) -> Vec<u8> {
    assert!(offset <= 0x00FF_FFFF, "Offset must fit in 3 bytes");
    assert!(data.len() <= 0x00FF_FFFF, "Length must fit in 3 bytes");
    let mut cmd = Vec::with_capacity(7 + data.len());
    cmd.push(DESFIRE_CMD_WRITE_DATA);
    cmd.push(file_no);
    cmd.extend_from_slice(&offset.to_le_bytes()[..3]);
    cmd.extend_from_slice(&(data.len() as u32).to_le_bytes()[..3]);
    cmd.extend_from_slice(data);
    cmd
}

/// Wrap a `SAM_ChangeKeyPICC` response payload into the native `ChangeKey`
/// command frame to relay to the card: `C4 <keyNo> <samPayload...>`.
///
/// HARDWARE-VERIFY (significant — see also [`build_change_key_picc`]): this
/// assumes `SAM_ChangeKeyPICC`'s response data (everything before the
/// trailing `90 00`) is exactly the cryptogram/CRC payload that goes after
/// `<keyNo>` in the native `ChangeKey` command, ready to relay verbatim —
/// i.e. the SAM does the crypto but the host still frames the native
/// command. This is the interpretation used by
/// `personalize::personalize_fob`; it has **not** been confirmed against
/// real SAM AV3 + DESFire hardware. An equally plausible alternative (seen
/// in some SAM command families) is that `SAM_ChangeKeyPICC` returns the
/// fully-framed native command including the `C4 <keyNo>` prefix itself, in
/// which case this function would double that prefix — check the SAM's
/// actual response layout on a bench before trusting this against a real
/// card.
///
/// SUPERSEDED — see the `SAM_ChangeKeyPICC` section-level doc comment; no
/// active flow calls this any more (personalization now builds+encrypts the
/// `ChangeKey` cryptogram itself via [`build_picc_change_key_header`] +
/// [`crypto::DesfireEv2Session`]).
#[allow(dead_code)]
pub fn build_picc_change_key_from_sam_payload(key_no: u8, sam_payload: &[u8]) -> Vec<u8> {
    let mut cmd = Vec::with_capacity(2 + sam_payload.len());
    cmd.push(DESFIRE_CMD_CHANGE_KEY);
    cmd.push(key_no);
    cmd.extend_from_slice(sam_payload);
    cmd
}

/// Build the native `ChangeKey` command's PLAIN header (`C4 <KeyNo>`) — the
/// portion that stays unencrypted in a `CommMode.Full` `ChangeKey`, used as
/// both the MAC input's `cmd_and_header` and the wire prefix (see
/// [`crypto::DesfireEv2Session::mac_command`],
/// `personalize::personalize_fob` step 5 — bead 4ty rework 2026-07-15).
pub fn build_picc_change_key_header(key_no: u8) -> [u8; 2] {
    [DESFIRE_CMD_CHANGE_KEY, key_no]
}

/// Build the native `WriteData` command's PLAIN header (everything except
/// the data payload): `3D <FileNo> <Offset[3], LSB-first> <Length[3],
/// LSB-first>`. Split out from [`build_picc_write_data`] so a
/// `CommMode.Full` caller can encrypt the data separately, then assemble
/// `header || encData || MAC` — see
/// [`crypto::DesfireEv2Session::{encrypt_full,mac_command}`](crypto::DesfireEv2Session),
/// `personalize::personalize_fob` step 6b.
///
/// HARDWARE-VERIFY: `length` is assumed to be the PLAINTEXT data length
/// (this project's reading of the commonly-published DESFire `WriteData`
/// layout); some references instead expect the on-wire/encrypted length
/// here — unconfirmed. For this project's fixed 16-byte token-id (already
/// block-aligned) the two coincide, so this ambiguity has no effect today,
/// but would matter for any future non-block-aligned payload.
pub fn build_picc_write_data_header(file_no: u8, offset: u32, length: u32) -> Vec<u8> {
    assert!(offset <= 0x00FF_FFFF, "Offset must fit in 3 bytes");
    assert!(length <= 0x00FF_FFFF, "Length must fit in 3 bytes");
    let mut cmd = Vec::with_capacity(7);
    cmd.push(DESFIRE_CMD_WRITE_DATA);
    cmd.push(file_no);
    cmd.extend_from_slice(&offset.to_le_bytes()[..3]);
    cmd.extend_from_slice(&length.to_le_bytes()[..3]);
    cmd
}

/// Parse a generic native PICC response of the form `<status> || data`,
/// checking the leading status byte against `expected_status` ([`PICC_OK`]
/// for a bare acknowledgement). Any other status byte — including any
/// DESFire native error code — is a hard failure. Distinct from
/// [`parse_picc_frame_ek_rnd_b`]/[`parse_picc_frame_pd_resp`] (fixed-length
/// EV2-handshake frames): this accepts any trailing data length, which the
/// generic personalization commands need (a `ChangeKey` confirmation
/// payload is not fixed-length in every published description).
pub fn parse_picc_native_status(resp: &[u8], expected_status: u8) -> Result<&[u8], SamError> {
    if resp.is_empty() {
        return Err(SamError::UnexpectedLength {
            got: 0,
            expected: 1,
        });
    }
    if resp[0] != expected_status {
        return Err(SamError::PiccStatus(resp[0]));
    }
    Ok(&resp[1..])
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn authenticate_host_pass1_framing() {
        let apdu = build_authenticate_host_pass1(0x01, 0x02, HOST_MODE_FULL);
        assert_eq!(
            apdu,
            vec![0x80, 0xA4, 0x00, 0x00, 0x03, 0x01, 0x02, 0x02, 0x00]
        );
    }

    #[test]
    fn authenticate_host_pass1_response_parses_continuation() {
        let mut resp = vec![0x11u8; 12];
        resp.push(0x90);
        resp.push(0xAF);
        let rnd2 = parse_authenticate_host_pass1_response(&resp).unwrap();
        assert_eq!(rnd2, [0x11u8; 12]);
    }

    #[test]
    fn authenticate_host_pass1_response_rejects_error_status() {
        let mut resp = vec![0x11u8; 12];
        resp.push(0x6A);
        resp.push(0x82);
        let err = parse_authenticate_host_pass1_response(&resp).unwrap_err();
        assert!(matches!(err, SamError::Status(0x6A82)));
    }

    #[test]
    fn authenticate_host_pass1_response_rejects_wrong_length() {
        let err = parse_authenticate_host_pass1_response(&[0u8; 5]).unwrap_err();
        assert!(matches!(
            err,
            SamError::UnexpectedLength {
                got: 5,
                expected: 14
            }
        ));
    }

    #[test]
    fn authenticate_host_pass2_framing() {
        let cmac2 = [0xAAu8; 8];
        let rnd1 = [0xBBu8; 12];
        let apdu = build_authenticate_host_pass2(&cmac2, &rnd1);
        let mut expected = vec![0x80, 0xA4, 0x00, 0x00, 0x14];
        expected.extend_from_slice(&cmac2);
        expected.extend_from_slice(&rnd1);
        expected.push(0x00);
        assert_eq!(apdu, expected);
        assert_eq!(apdu.len(), 26);
    }

    #[test]
    fn authenticate_host_pass2_response_parses_continuation_and_extracts_rnd_bc() {
        let mut resp = vec![0x01u8; 8]; // unverified prefix (HARDWARE-VERIFY)
        resp.extend_from_slice(&[0x22u8; 16]); // EncKex(RndB)
        resp.push(0x90);
        resp.push(0xAF);
        let rnd_bc = parse_authenticate_host_pass2_response(&resp).unwrap();
        assert_eq!(rnd_bc, [0x22u8; 16]);
    }

    #[test]
    fn authenticate_host_pass2_response_rejects_error_status() {
        let mut resp = vec![0u8; 24];
        resp.push(0x6A);
        resp.push(0x82);
        let err = parse_authenticate_host_pass2_response(&resp).unwrap_err();
        assert!(matches!(err, SamError::Status(0x6A82)));
    }

    #[test]
    fn authenticate_host_pass2_response_rejects_wrong_length() {
        let err = parse_authenticate_host_pass2_response(&[0u8; 10]).unwrap_err();
        assert!(matches!(
            err,
            SamError::UnexpectedLength {
                got: 10,
                expected: 26
            }
        ));
    }

    #[test]
    fn authenticate_host_pass3_framing() {
        let ecipher = [0x33u8; 32];
        let apdu = build_authenticate_host_pass3(&ecipher);
        let mut expected = vec![0x80, 0xA4, 0x00, 0x00, 32];
        expected.extend_from_slice(&ecipher);
        expected.push(0x00);
        assert_eq!(apdu, expected);
        assert_eq!(apdu.len(), 38); // 4 header + 1 Lc + 32 data + 1 Le
    }

    #[test]
    fn authenticate_host_pass3_response_accepts_9000_and_rejects_others() {
        assert!(parse_authenticate_host_pass3_response(&[0x90, 0x00]).is_ok());
        let err = parse_authenticate_host_pass3_response(&[0x91, 0xAE]).unwrap_err();
        assert!(matches!(err, SamError::Status(0x91AE)));
    }

    // -------------------------------------------------------------
    // SAM_LockUnlock (Unlock, state 0x00).
    // -------------------------------------------------------------

    #[test]
    fn lock_unlock_pass1_framing_matches_hardware_confirmed_bytes() {
        // Empirically confirmed on real SAM AV3 hardware: keyNo=0, keyVer=0.
        let apdu = build_lock_unlock_pass1(LOCK_UNLOCK_STATE_UNLOCK, 0x00, 0x00);
        assert_eq!(apdu, vec![0x80, 0x10, 0x00, 0x00, 0x02, 0x00, 0x00, 0x00]);
    }

    #[test]
    fn lock_unlock_pass1_framing_with_nonzero_key_ref() {
        let apdu = build_lock_unlock_pass1(LOCK_UNLOCK_STATE_UNLOCK, 0x03, 0x07);
        assert_eq!(apdu, vec![0x80, 0x10, 0x00, 0x00, 0x02, 0x03, 0x07, 0x00]);
    }

    #[test]
    fn lock_unlock_pass1_response_parses_continuation() {
        // Empirically confirmed response bytes for pass 1.
        let resp = hex::decode("DC284AD3D2222C6D8378B93A90AF").unwrap();
        let rnd2 = parse_lock_unlock_pass1_response(&resp).unwrap();
        assert_eq!(
            rnd2,
            hex::decode("DC284AD3D2222C6D8378B93A").unwrap().as_slice()
        );
    }

    #[test]
    fn lock_unlock_pass1_response_rejects_error_status() {
        let mut resp = vec![0u8; 12];
        resp.push(0x69);
        resp.push(0x85); // conditions of use not satisfied
        let err = parse_lock_unlock_pass1_response(&resp).unwrap_err();
        assert!(matches!(err, SamError::Status(0x6985)));
    }

    #[test]
    fn lock_unlock_pass2_framing() {
        let mac_host = [0xAAu8; 8];
        let rnd1 = [0xBBu8; 12];
        let apdu = build_lock_unlock_pass2(&mac_host, &rnd1);
        let mut expected = vec![0x80, 0x10, 0x00, 0x00, 0x14];
        expected.extend_from_slice(&mac_host);
        expected.extend_from_slice(&rnd1);
        expected.push(0x00);
        assert_eq!(apdu, expected);
        assert_eq!(apdu.len(), 26); // 4 header + 1 Lc + 20 data + 1 Le
    }

    #[test]
    fn lock_unlock_pass2_response_parses_mac_sam_and_enc_rnd_b() {
        let mac_sam = [0x11u8; 8];
        let enc_rnd_b = [0x22u8; 16];
        let mut resp = mac_sam.to_vec();
        resp.extend_from_slice(&enc_rnd_b);
        resp.push(0x90);
        resp.push(0xAF);
        let (parsed_mac, parsed_enc_rnd_b) = parse_lock_unlock_pass2_response(&resp).unwrap();
        assert_eq!(parsed_mac, mac_sam);
        assert_eq!(parsed_enc_rnd_b, enc_rnd_b);
    }

    #[test]
    fn lock_unlock_pass2_response_rejects_error_status() {
        let mut resp = vec![0u8; 24];
        resp.push(0x6A);
        resp.push(0x82);
        let err = parse_lock_unlock_pass2_response(&resp).unwrap_err();
        assert!(matches!(err, SamError::Status(0x6A82)));
    }

    #[test]
    fn lock_unlock_pass2_response_rejects_wrong_length() {
        let err = parse_lock_unlock_pass2_response(&[0u8; 10]).unwrap_err();
        assert!(matches!(
            err,
            SamError::UnexpectedLength {
                got: 10,
                expected: 26
            }
        ));
    }

    #[test]
    fn lock_unlock_pass3_framing() {
        let ecipher = [0xCDu8; 32];
        let apdu = build_lock_unlock_pass3(&ecipher);
        let mut expected = vec![0x80, 0x10, 0x00, 0x00, 32];
        expected.extend_from_slice(&ecipher);
        expected.push(0x00);
        assert_eq!(apdu, expected);
        assert_eq!(apdu.len(), 5 + 32 + 1);
    }

    #[test]
    fn lock_unlock_pass3_response_accepts_9000_and_rejects_others() {
        assert!(parse_lock_unlock_pass3_response(&[0x90, 0x00]).is_ok());
        let err = parse_lock_unlock_pass3_response(&[0x91, 0xAE]).unwrap_err();
        assert!(matches!(err, SamError::Status(0x91AE)));
    }

    #[test]
    fn change_key_entry_header_framing() {
        let header = build_change_key_entry_header(0x03, pro_mas::VER_A | pro_mas::SET);
        assert_eq!(header, [0x80, 0xC1, 0x03, 0x82]);
    }

    #[test]
    fn pro_mas_bit_positions_match_spec() {
        // 80=VerA,40=VerB,20=VerC,10=AID+KeyNo,08=CEK,04=KUC,02=SET,01=keyVer-sep
        assert_eq!(pro_mas::VER_A, 0x80);
        assert_eq!(pro_mas::VER_B, 0x40);
        assert_eq!(pro_mas::VER_C, 0x20);
        assert_eq!(pro_mas::AID_KEYNO, 0x10);
        assert_eq!(pro_mas::CEK, 0x08);
        assert_eq!(pro_mas::KUC, 0x04);
        assert_eq!(pro_mas::SET, 0x02);
        assert_eq!(pro_mas::KEYVER_SEP, 0x01);
        // All bits distinct (no accidental overlap).
        let all = [
            pro_mas::VER_A,
            pro_mas::VER_B,
            pro_mas::VER_C,
            pro_mas::AID_KEYNO,
            pro_mas::CEK,
            pro_mas::KUC,
            pro_mas::SET,
            pro_mas::KEYVER_SEP,
        ];
        let combined: u8 = all.iter().fold(0u8, |acc, b| acc | b);
        let sum_of_bits: u32 = all.iter().map(|b| b.count_ones()).sum();
        assert_eq!(
            combined.count_ones(),
            sum_of_bits,
            "pro_mas bits must not overlap"
        );
    }

    #[test]
    fn set_flags_match_spec_examples() {
        // "Company PICC-master w/o requireAuth = SET `20 00`"
        let set = set_flags::AES128_TYPE;
        assert_eq!(set.to_le_bytes(), [0x20, 0x00]);

        // "(`21 00` to also allow DumpSessionKey)"
        let set = set_flags::AES128_TYPE | set_flags::ALLOW_DUMP_SESSION_KEY;
        assert_eq!(set.to_le_bytes(), [0x21, 0x00]);
    }

    #[test]
    fn set_flags_bit_positions_match_spec() {
        // bit0=allowDumpSessionKey, bit2=keepIV, bit5=AES128-type,
        // bit7=requireAuth, bit9=disableKeyEntry.
        assert_eq!(set_flags::ALLOW_DUMP_SESSION_KEY, 0x0001);
        assert_eq!(set_flags::KEEP_IV, 0x0004);
        assert_eq!(set_flags::AES128_TYPE, 0x0020);
        assert_eq!(set_flags::REQUIRE_AUTH, 0x0080);
        assert_eq!(set_flags::DISABLE_KEY_ENTRY, 0x0200);
    }

    #[test]
    fn ext_set_flags_key_class_values_match_spec() {
        // KeyClass occupies bits 0-2 and is NOT a dense 0..=3 enum:
        // HOST=0, PICC=1, OfflineChange=2, OfflineCrypto=4 (skips 3).
        assert_eq!(ext_set_flags::KEY_CLASS_HOST, 0);
        assert_eq!(ext_set_flags::KEY_CLASS_PICC, 1);
        assert_eq!(ext_set_flags::KEY_CLASS_OFFLINE_CHANGE, 2);
        assert_eq!(ext_set_flags::KEY_CLASS_OFFLINE_CRYPTO, 4);
        assert_eq!(ext_set_flags::ALLOW_DUMP_SECRET_KEY, 0x08);
        assert_eq!(ext_set_flags::RESTRICT_TO_DIVERSIFIED_USE, 0x10);
    }

    #[test]
    fn ext_set_flags_match_spec_examples() {
        // "PICC-master diversified-only = ExtSET `11`"
        let ext_set = ext_set_flags::KEY_CLASS_PICC | ext_set_flags::RESTRICT_TO_DIVERSIFIED_USE;
        assert_eq!(ext_set, 0x11);

        // "(with export = `19`)"
        let ext_set = ext_set_flags::KEY_CLASS_PICC
            | ext_set_flags::ALLOW_DUMP_SECRET_KEY
            | ext_set_flags::RESTRICT_TO_DIVERSIFIED_USE;
        assert_eq!(ext_set, 0x19);
    }

    #[test]
    fn key_entry_serializes_to_exactly_61_bytes_with_correct_offsets() {
        let entry = AesKeyEntry {
            key_va: [0xAA; 16],
            key_vb: [0xBB; 16],
            key_vc: [0xCC; 16],
            df_aid: [0x01, 0x02, 0x03],
            df_key_no: 0x04,
            key_no_cek: 0x05,
            key_ver_cek: 0x06,
            ref_no_kuc: 0x07,
            set: set_flags::AES128_TYPE | set_flags::ALLOW_DUMP_SESSION_KEY, // -> 21 00
            ver_a: 0x10,
            ver_b: 0x11,
            ver_c: 0x12,
            ext_set: ext_set_flags::KEY_CLASS_PICC
                | ext_set_flags::ALLOW_DUMP_SECRET_KEY
                | ext_set_flags::RESTRICT_TO_DIVERSIFIED_USE, // -> 19
        };
        let bytes = entry.to_bytes();
        assert_eq!(bytes.len(), AesKeyEntry::LEN);
        assert_eq!(bytes.len(), 61);

        let mut expected = Vec::with_capacity(61);
        expected.extend_from_slice(&[0xAA; 16]);
        expected.extend_from_slice(&[0xBB; 16]);
        expected.extend_from_slice(&[0xCC; 16]);
        expected.extend_from_slice(&[0x01, 0x02, 0x03]);
        expected.push(0x04);
        expected.push(0x05);
        expected.push(0x06);
        expected.push(0x07);
        expected.extend_from_slice(&[0x21, 0x00]); // SET, LE
        expected.push(0x10);
        expected.push(0x11);
        expected.push(0x12);
        expected.push(0x19); // ExtSET
        assert_eq!(bytes.to_vec(), expected);
    }

    #[test]
    fn change_key_entry_full_from_entry_produces_well_formed_apdu() {
        let entry = AesKeyEntry {
            key_va: [0x01; 16],
            key_vb: [0x02; 16],
            key_vc: [0x03; 16],
            df_aid: [0xAA, 0xBB, 0xCC],
            df_key_no: 0x00,
            key_no_cek: 0x00,
            key_ver_cek: 0x00,
            ref_no_kuc: 0xFF,
            set: set_flags::AES128_TYPE,
            ver_a: 0x00,
            ver_b: 0x00,
            ver_c: 0x00,
            ext_set: ext_set_flags::KEY_CLASS_PICC | ext_set_flags::RESTRICT_TO_DIVERSIFIED_USE,
        };
        let ke = [0x11u8; 16];
        let km = [0x22u8; 16];
        let apdu =
            build_change_key_entry_full_from_entry(0x05, pro_mas::VER_A, &ke, &km, &entry).unwrap();

        // Header
        assert_eq!(&apdu[0..4], &[0x80, 0xC1, 0x05, pro_mas::VER_A]);
        // Lc = enc(64 bytes, padded from 61) + mac8(8) = 72
        assert_eq!(apdu[4], 72);
        // Total length: header(4) + Lc-byte(1) + Lc(72), NO trailing Le.
        assert_eq!(apdu.len(), 4 + 1 + 72);

        // Deterministic: same inputs -> same bytes.
        let apdu2 =
            build_change_key_entry_full_from_entry(0x05, pro_mas::VER_A, &ke, &km, &entry).unwrap();
        assert_eq!(apdu, apdu2);
    }

    fn sample_offline_entry() -> AesKeyEntry {
        AesKeyEntry {
            key_va: [0x99; 16],
            key_vb: [0u8; 16],
            key_vc: [0u8; 16],
            df_aid: [0x00, 0x00, 0x00],
            df_key_no: 0x00,
            key_no_cek: 0x00,
            key_ver_cek: 0x00,
            ref_no_kuc: 0xFF,
            set: set_flags::AES128_TYPE,
            ver_a: 0x00,
            ver_b: 0x00,
            ver_c: 0x00,
            ext_set: ext_set_flags::KEY_CLASS_PICC | ext_set_flags::RESTRICT_TO_DIVERSIFIED_USE,
        }
    }

    #[test]
    fn build_encke_offline_is_encdata_plus_mac_stripped_of_header_and_le() {
        // encke must equal the Full-protection wrapped frame with its 5-byte
        // header and trailing Le removed — self-consistent against the
        // byte-exact `create_full_protection_cmd` this reuses.
        let entry = sample_offline_entry();
        let ke = [0x11u8; 16];
        let km = [0x22u8; 16];

        let encke = build_encke_offline(0x01, pro_mas::VER_A, &ke, &km, &entry).unwrap();
        // 61-byte entry -> method-2 padded to 64 (encData) + 8-byte MAC.
        assert_eq!(encke.len(), 64 + 8);

        let wrapped =
            build_change_key_entry_full_from_entry(0x01, pro_mas::VER_A, &ke, &km, &entry).unwrap();
        // wrapped = [hdr(5)][encData(64)][mac(8)] (no Le); encke = the 72-byte tail.
        assert_eq!(encke, wrapped[5..]);

        // Deterministic.
        let encke2 = build_encke_offline(0x01, pro_mas::VER_A, &ke, &km, &entry).unwrap();
        assert_eq!(encke, encke2);
    }

    #[test]
    fn build_change_key_entry_offline_framing_matches_reference() {
        // Reference (samav2iso7816commands.cpp:720-742):
        // {80 C1 keyno proMas Lc=(2+encke) changecnt_hi changecnt_lo encke...}
        // no Le.
        let encke = vec![0xEE; 72];
        let cmd = build_change_key_entry_offline(0x01, pro_mas::VER_A, 0x1234, &encke);

        assert_eq!(cmd[0], 0x80); // CLA
        assert_eq!(cmd[1], 0xC1); // INS ChangeKeyEntry
        assert_eq!(cmd[2], 0x01); // P1 = entryNo
        assert_eq!(cmd[3], pro_mas::VER_A); // P2 = proMas
        assert_eq!(cmd[4], (2 + 72) as u8); // Lc = 74
        assert_eq!(cmd[5], 0x12); // changecnt hi (big-endian)
        assert_eq!(cmd[6], 0x34); // changecnt lo
        assert_eq!(&cmd[7..], &encke[..]); // encke follows
        assert_eq!(cmd.len(), 5 + 2 + 72); // NO trailing Le
    }

    #[test]
    fn build_change_key_entry_offline_changecnt_is_big_endian() {
        let encke = vec![0x00; 4];
        let cmd = build_change_key_entry_offline(0x01, 0x00, 0x00FF, &encke);
        assert_eq!(cmd[5], 0x00); // hi
        assert_eq!(cmd[6], 0xFF); // lo
    }

    #[test]
    fn change_key_picc_framing() {
        let apdu = build_change_key_picc(CHANGE_KEY_PICC_APP_KEY_DIV, &[0xDE, 0xAD, 0xBE, 0xEF]);
        assert_eq!(
            apdu,
            vec![0x80, 0xC4, 0x22, 0x00, 0x04, 0xDE, 0xAD, 0xBE, 0xEF, 0x00]
        );

        // App-master-key diversification uses P1 = 0x23.
        let apdu2 =
            build_change_key_picc(CHANGE_KEY_PICC_APP_MASTER_DIV, &[0xDE, 0xAD, 0xBE, 0xEF]);
        assert_eq!(apdu2[2], 0x23);
    }

    #[test]
    fn authenticate_picc_p1_values_match_spec() {
        // 0x80=EV2 First (key by entry), 0xC0=NonFirst,
        // 0x03=EV1 w/div, 0x00=EV1 no div.
        assert_eq!(AUTH_PICC_P1_EV2_FIRST, 0x80);
        assert_eq!(AUTH_PICC_P1_EV2_NON_FIRST, 0xC0);
        assert_eq!(AUTH_PICC_P1_EV1_WITH_DIV, 0x03);
        assert_eq!(AUTH_PICC_P1_EV1_NO_DIV, 0x00);
        // Each P1 value is accepted as-is by the pass-1 builder (P1 is just
        // forwarded, not validated) — this locks in that framing contract
        // for every mode this module defines, even the ones no flow in this
        // step exercises yet (NonFirst / EV1).
        for p1 in [
            AUTH_PICC_P1_EV2_FIRST,
            AUTH_PICC_P1_EV2_NON_FIRST,
            AUTH_PICC_P1_EV1_WITH_DIV,
            AUTH_PICC_P1_EV1_NO_DIV,
        ] {
            let apdu = build_authenticate_picc_pass1(p1, 0x00, 0x00, &[0u8; 16]);
            assert_eq!(apdu[2], p1);
        }
    }

    #[test]
    fn picc_native_command_opcodes_match_spec() {
        assert_eq!(PICC_CMD_AUTHENTICATE_EV2_FIRST, 0x71);
        // HARDWARE-VERIFY: see doc comment on the constant itself.
        assert_eq!(PICC_CMD_AUTHENTICATE_EV2_NON_FIRST, 0x77);
        assert_eq!(PICC_CMD_AUTHENTICATE_AES_EV1, 0xAA);
    }

    #[test]
    fn authenticate_picc_pass1_framing() {
        let ek_rnd_b = [0x11u8; 16];
        let apdu = build_authenticate_picc_pass1(AUTH_PICC_P1_EV2_FIRST, 0x01, 0x02, &ek_rnd_b);
        let mut expected = vec![0x80, 0x0A, 0x80, 0x00, 19, 0x01, 0x02, 0x00];
        expected.extend_from_slice(&ek_rnd_b);
        // No trailing Le (bead eyc, 2026-07-15 fix — pre-existing test bug:
        // this assertion still expected the stray Le the fix removed).
        assert_eq!(apdu, expected);
        assert_eq!(apdu.len(), 24);
    }

    #[test]
    fn authenticate_picc_pass1_response_parses_continuation() {
        let mut resp = vec![0xAAu8; 32];
        resp.push(0x90);
        resp.push(0xAF);
        let parsed = parse_authenticate_picc_pass1_response(&resp).unwrap();
        assert_eq!(parsed, [0xAAu8; 32]);
    }

    #[test]
    fn authenticate_picc_pass1_response_rejects_error_status() {
        let mut resp = vec![0xAAu8; 32];
        resp.push(0x6A);
        resp.push(0x82);
        let err = parse_authenticate_picc_pass1_response(&resp).unwrap_err();
        assert!(matches!(err, SamError::Status(0x6A82)));
    }

    #[test]
    fn authenticate_picc_pass2_framing() {
        let pd_resp = [0x22u8; 32];
        let apdu = build_authenticate_picc_pass2(&pd_resp);
        let mut expected = vec![0x80, 0x0A, 0x00, 0x00, 32];
        expected.extend_from_slice(&pd_resp);
        expected.push(0x00);
        assert_eq!(apdu, expected);
        assert_eq!(apdu.len(), 38);
    }

    #[test]
    fn authenticate_picc_pass2_response_parses_success() {
        let mut resp = vec![0x01, 0x02, 0x03];
        resp.push(0x90);
        resp.push(0x00);
        let caps = parse_authenticate_picc_pass2_response(&resp).unwrap();
        assert_eq!(caps, vec![0x01, 0x02, 0x03]);
    }

    #[test]
    fn authenticate_picc_pass2_response_rejects_error_status() {
        let mut resp = vec![0x01, 0x02, 0x03];
        resp.push(0x6A);
        resp.push(0x82);
        let err = parse_authenticate_picc_pass2_response(&resp).unwrap_err();
        assert!(matches!(err, SamError::Status(0x6A82)));
    }

    #[test]
    fn picc_ev2_first_and_continue_framing() {
        assert_eq!(
            build_picc_authenticate_ev2_first(0x02),
            vec![0x71, 0x02, 0x00]
        );
        assert_eq!(build_picc_authenticate_ev1(0x02), vec![0xAA, 0x02, 0x00]);
        let data = [0x11u8; 32];
        let mut expected = vec![0xAF];
        expected.extend_from_slice(&data);
        assert_eq!(build_picc_continue(&data), expected);
    }

    #[test]
    fn picc_frame_ek_rnd_b_parses_expected_status_and_rejects_other() {
        let mut resp = vec![PICC_AF];
        resp.extend_from_slice(&[0x33u8; 16]);
        assert_eq!(
            parse_picc_frame_ek_rnd_b(&resp, PICC_AF).unwrap(),
            [0x33u8; 16]
        );
        // Wrong expected status -> hard failure, fail-closed.
        let err = parse_picc_frame_ek_rnd_b(&resp, PICC_OK).unwrap_err();
        assert!(matches!(err, SamError::PiccStatus(0xAF)));
    }

    #[test]
    fn picc_frame_ek_rnd_b_rejects_wrong_length() {
        let err = parse_picc_frame_ek_rnd_b(&[0x00u8; 10], PICC_AF).unwrap_err();
        assert!(matches!(
            err,
            SamError::UnexpectedLength {
                got: 10,
                expected: 17
            }
        ));
    }

    #[test]
    fn picc_frame_pd_resp_parses_expected_status_and_rejects_other() {
        let mut resp = vec![PICC_OK];
        resp.extend_from_slice(&[0x44u8; 32]);
        assert_eq!(
            parse_picc_frame_pd_resp(&resp, PICC_OK).unwrap(),
            [0x44u8; 32]
        );
        let err = parse_picc_frame_pd_resp(&resp, PICC_AF).unwrap_err();
        assert!(matches!(err, SamError::PiccStatus(0x00)));
    }

    #[test]
    fn picc_frame_pd_resp_rejects_wrong_length() {
        let err = parse_picc_frame_pd_resp(&[0x00u8; 10], PICC_OK).unwrap_err();
        assert!(matches!(
            err,
            SamError::UnexpectedLength {
                got: 10,
                expected: 33
            }
        ));
    }

    #[test]
    fn apply_sm_and_remove_sm_header_framing() {
        let apply = build_apply_sm(
            COMM_MODE_FULL,
            &[
                DESFIRE_CMD_READ_DATA,
                0x01,
                0x00,
                0x00,
                0x00,
                0x00,
                0x00,
                0x00,
            ],
        );
        assert_eq!(&apply[0..4], &[0x80, 0xAE, 0x00, COMM_MODE_FULL]);
        assert_eq!(apply[4], 8); // Lc
        assert_eq!(*apply.last().unwrap(), 0x00); // Le

        let remove = build_remove_sm(&[0xABu8; 10]);
        assert_eq!(&remove[0..4], &[0x80, 0xAD, 0x00, COMM_MODE_FULL]);
        assert_eq!(remove[4], 10); // Lc
        assert_eq!(*remove.last().unwrap(), 0x00); // Le
    }

    #[test]
    fn apply_sm_supports_every_comm_mode() {
        // Apply_SM must be callable even in plain mode, to keep the SAM's
        // R/W counters synced with the card (see doc comment).
        for mode in [COMM_MODE_PLAIN, COMM_MODE_MAC, COMM_MODE_FULL] {
            let apdu = build_apply_sm(mode, &[DESFIRE_CMD_READ_DATA]);
            assert_eq!(apdu[3], mode);
        }
        assert_eq!(COMM_MODE_PLAIN, 0x00);
        assert_eq!(COMM_MODE_MAC, 0x10);
        assert_eq!(COMM_MODE_FULL, 0x30);
    }

    #[test]
    fn parse_status_ok_accepts_9000_and_rejects_others() {
        let resp = vec![0x01, 0x02, 0x90, 0x00];
        assert_eq!(parse_status_ok(&resp).unwrap(), &[0x01, 0x02]);

        let resp_err = vec![0x01, 0x02, 0x6A, 0x82];
        let err = parse_status_ok(&resp_err).unwrap_err();
        assert!(matches!(err, SamError::Status(0x6A82)));
    }

    // -------------------------------------------------------------
    // DESFire-native personalization command builders (bead 4ty).
    // -------------------------------------------------------------

    #[test]
    fn select_application_framing() {
        let apdu = build_picc_select_application(&[0x01, 0x02, 0x03]);
        assert_eq!(apdu, vec![0x5A, 0x01, 0x02, 0x03]);
    }

    #[test]
    fn create_application_framing() {
        let apdu = build_picc_create_application(
            &[0xAA, 0xBB, 0xCC],
            key_settings1::ALLOW_CHANGE_MASTER_KEY,
            KEY_SETTINGS2_AES | 0x01,
        );
        assert_eq!(
            apdu,
            vec![
                0xCA,
                0xAA,
                0xBB,
                0xCC,
                key_settings1::ALLOW_CHANGE_MASTER_KEY,
                KEY_SETTINGS2_AES | 0x01
            ]
        );
    }

    #[test]
    fn key_settings_constants_match_spec() {
        assert_eq!(key_settings1::ALLOW_CHANGE_MASTER_KEY, 0x01);
        assert_eq!(key_settings1::FREE_DIRECTORY_LIST_WITHOUT_MASTER_KEY, 0x02);
        assert_eq!(key_settings1::FREE_CREATE_DELETE_WITHOUT_MASTER_KEY, 0x04);
        assert_eq!(key_settings1::CONFIGURATION_CHANGEABLE, 0x08);
        assert_eq!(KEY_SETTINGS2_AES, 0x80);
    }

    #[test]
    fn create_std_data_file_framing_is_little_endian() {
        let apdu =
            build_picc_create_std_data_file(0x02, file_comm_settings::ENCIPHERED, 0xE000, 16);
        assert_eq!(
            apdu,
            vec![
                0xCD,
                0x02,
                file_comm_settings::ENCIPHERED,
                0x00,
                0xE0, // access rights LE
                0x10,
                0x00,
                0x00, // file size LE (16)
            ]
        );
    }

    #[test]
    fn file_comm_settings_match_spec() {
        assert_eq!(file_comm_settings::PLAIN, 0x00);
        assert_eq!(file_comm_settings::MACED, 0x01);
        assert_eq!(file_comm_settings::ENCIPHERED, 0x03);
    }

    #[test]
    fn write_data_framing_is_little_endian_with_trailing_payload() {
        let data = [0xAB; 16];
        let apdu = build_picc_write_data(0x02, 0, &data);
        let mut expected = vec![0x3D, 0x02, 0x00, 0x00, 0x00, 0x10, 0x00, 0x00];
        expected.extend_from_slice(&data);
        assert_eq!(apdu, expected);
    }

    #[test]
    fn write_data_framing_with_nonzero_offset() {
        let data = [0x01, 0x02];
        let apdu = build_picc_write_data(0x03, 0x0102, &data);
        // Offset 0x000102 LE = 02 01 00.
        assert_eq!(&apdu[2..5], &[0x02, 0x01, 0x00]);
        // Length 2 LE = 02 00 00.
        assert_eq!(&apdu[5..8], &[0x02, 0x00, 0x00]);
        assert_eq!(&apdu[8..], &data);
    }

    #[test]
    fn change_key_from_sam_payload_framing() {
        let apdu = build_picc_change_key_from_sam_payload(0x00, &[0x11, 0x22, 0x33]);
        assert_eq!(apdu, vec![0xC4, 0x00, 0x11, 0x22, 0x33]);
    }

    #[test]
    fn desfire_native_command_bytes_match_spec() {
        assert_eq!(DESFIRE_CMD_SELECT_APPLICATION, 0x5A);
        assert_eq!(DESFIRE_CMD_CREATE_APPLICATION, 0xCA);
        assert_eq!(DESFIRE_CMD_CREATE_STD_DATA_FILE, 0xCD);
        assert_eq!(DESFIRE_CMD_WRITE_DATA, 0x3D);
        assert_eq!(DESFIRE_CMD_CHANGE_KEY, 0xC4);
    }

    #[test]
    fn parse_picc_native_status_accepts_expected_and_rejects_other() {
        let resp = vec![PICC_OK, 0xAA, 0xBB];
        assert_eq!(
            parse_picc_native_status(&resp, PICC_OK).unwrap(),
            &[0xAA, 0xBB]
        );

        let err = parse_picc_native_status(&resp, PICC_AF).unwrap_err();
        assert!(matches!(err, SamError::PiccStatus(0x00)));
    }

    #[test]
    fn parse_picc_native_status_rejects_empty_response() {
        let err = parse_picc_native_status(&[], PICC_OK).unwrap_err();
        assert!(matches!(
            err,
            SamError::UnexpectedLength {
                got: 0,
                expected: 1
            }
        ));
    }

    // -------------------------------------------------------------
    // Daily punch authentication builders (bead bi0).
    // -------------------------------------------------------------

    #[test]
    fn read_data_framing_is_little_endian() {
        let apdu = build_picc_read_data(0x01, 0x00_0102, 0x10);
        assert_eq!(
            apdu,
            vec![
                DESFIRE_CMD_READ_DATA,
                0x01,
                0x02,
                0x01,
                0x00, // offset LE
                0x10,
                0x00,
                0x00, // length LE
            ]
        );
    }

    #[test]
    fn read_data_framing_matches_write_data_convention_for_zero_offset() {
        let apdu = build_picc_read_data(0x00, 0, 16);
        assert_eq!(
            apdu,
            vec![
                DESFIRE_CMD_READ_DATA,
                0x00,
                0x00,
                0x00,
                0x00,
                0x10,
                0x00,
                0x00
            ]
        );
    }

    #[test]
    fn authenticate_picc_p1_ev2_first_div_extends_ev2_first_with_div_bits() {
        // Bit pattern extrapolation documented on the constant itself: EV2
        // First (0x80) OR'd with the EV1 "with div" bits (0x03).
        assert_eq!(AUTH_PICC_P1_EV2_FIRST_DIV, 0x83);
        assert_eq!(
            AUTH_PICC_P1_EV2_FIRST_DIV,
            AUTH_PICC_P1_EV2_FIRST | AUTH_PICC_P1_EV1_WITH_DIV
        );
    }

    #[test]
    fn authenticate_picc_pass1_diversified_framing_appends_div_input() {
        let ek_rnd_b = [0x11u8; 16];
        let div_input = [0xAAu8; 11]; // UID[7] || KeyNo || AID[3]
        let apdu = build_authenticate_picc_pass1_diversified(0x05, 0x02, &ek_rnd_b, &div_input);

        let mut expected = vec![
            0x80,
            0x0A,
            AUTH_PICC_P1_EV2_FIRST_DIV,
            0x00,
            19 + 11, // Lc
            0x05,    // keyEntryNo
            0x02,    // keyVer
            0x00,    // authMode
        ];
        expected.extend_from_slice(&ek_rnd_b);
        expected.extend_from_slice(&div_input);
        // No trailing Le (bead eyc, 2026-07-15 fix — pre-existing test bug:
        // this assertion still expected the stray Le the fix removed).
        assert_eq!(apdu, expected);
        assert_eq!(apdu.len(), 5 + 19 + 11);
    }

    #[test]
    fn authenticate_picc_pass1_diversified_with_empty_div_input_matches_non_diversified_payload_shape(
    ) {
        // Sanity: with no div bytes, the payload before Le is identical in
        // shape to the non-diversified pass-1 builder (only P1 differs).
        let ek_rnd_b = [0x22u8; 16];
        let diversified = build_authenticate_picc_pass1_diversified(0x01, 0x00, &ek_rnd_b, &[]);
        let plain = build_authenticate_picc_pass1(AUTH_PICC_P1_EV2_FIRST, 0x01, 0x00, &ek_rnd_b);
        assert_eq!(diversified.len(), plain.len());
        assert_eq!(&diversified[3..], &plain[3..]); // everything but P1 matches
        assert_ne!(diversified[2], plain[2]); // P1 differs
    }

    /// The response parser for a diversified pass 1 is identical to the
    /// non-diversified one — the SAM's response shape doesn't depend on
    /// what we sent it, only on the outcome.
    #[test]
    fn authenticate_picc_pass1_diversified_response_reuses_existing_parser() {
        let mut resp = vec![0x55u8; 32];
        resp.push(0x90);
        resp.push(0xAF);
        let parsed = parse_authenticate_picc_pass1_response(&resp).unwrap();
        assert_eq!(parsed, [0x55u8; 32]);
    }

    // -----------------------------------------------------------------
    // SAM_DumpSecretKey (bead 4ty rework 2026-07-15).
    // -----------------------------------------------------------------

    #[test]
    fn dump_secret_key_plain_framing_with_div_input_sets_p1_and_trailing_le() {
        let div_input = [0xDEu8, 0xAD, 0xBE, 0xEF];
        let cmd = build_dump_secret_key_plain(0x02, 0x01, &div_input);
        // 80 D6 02(p1: diversified) 00 06(lc=2+4) 02(keyno) 01(keyver) <div> 00(Le).
        let mut expected = vec![
            0x80,
            INS_DUMP_SECRET_KEY,
            DUMP_SECRET_KEY_P1_DIVERSIFIED,
            0x00,
            0x06,
            0x02,
            0x01,
        ];
        expected.extend_from_slice(&div_input);
        expected.push(0x00); // Le — case-4, requests the diversified key back
        assert_eq!(cmd, expected);
        assert_eq!(
            cmd.len(),
            5 + 2 + div_input.len() + 1,
            "header + data + trailing Le byte"
        );
    }

    #[test]
    fn dump_secret_key_plain_with_empty_div_input_clears_p1_bit() {
        let cmd = build_dump_secret_key_plain(0x00, 0x00, &[]);
        assert_eq!(
            cmd,
            vec![
                0x80,
                INS_DUMP_SECRET_KEY,
                0x00,
                0x00,
                0x02,
                0x00,
                0x00,
                0x00
            ]
        );
    }

    #[test]
    fn dump_secret_key_plain_is_the_lc_data_le_shape_create_full_protection_cmd_accepts() {
        // SAM_DumpSecretKey is case-4 (LC DATA LE); `create_full_protection_cmd`
        // must accept it and carry the Le through — this must not error.
        let cmd = build_dump_secret_key_plain(0x02, 0x00, &[0x01u8; 11]);
        assert_eq!(*cmd.last().unwrap(), 0x00, "trailing Le present");
        let mut sm = crate::reader::sam::crypto::SecureMessaging::new([0u8; 16], [0u8; 16]);
        assert!(sm.create_full_protection_cmd(&cmd).is_ok());
    }

    // -----------------------------------------------------------------
    // Native ChangeKey/WriteData header builders (bead 4ty rework
    // 2026-07-15 — software CommMode.Full framing).
    // -----------------------------------------------------------------

    #[test]
    fn change_key_header_framing() {
        assert_eq!(build_picc_change_key_header(0x00), [0xC4, 0x00]);
        assert_eq!(build_picc_change_key_header(0x03), [0xC4, 0x03]);
    }

    #[test]
    fn write_data_header_framing_matches_write_data_prefix() {
        let header = build_picc_write_data_header(0x02, 0, 16);
        assert_eq!(header, vec![0x3D, 0x02, 0x00, 0x00, 0x00, 0x10, 0x00, 0x00]);
        // Matches the equivalent prefix of the data-inclusive builder.
        let full = build_picc_write_data(0x02, 0, &[0xABu8; 16]);
        assert_eq!(header, full[..8]);
    }

    #[test]
    fn write_data_header_framing_with_nonzero_offset() {
        let header = build_picc_write_data_header(0x03, 0x0102, 2);
        assert_eq!(&header[2..5], &[0x02, 0x01, 0x00]);
        assert_eq!(&header[5..8], &[0x02, 0x00, 0x00]);
    }
}
