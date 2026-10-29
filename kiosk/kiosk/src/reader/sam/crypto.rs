//! AES-128 crypto primitives for the SAM AV3 core: AES-CMAC (RFC 4493),
//! AES-CBC (no padding, block-aligned input only), AN10922 key
//! diversification, and the host-authentication session-key derivation.
//!
//! Everything here is a pure function — no I/O, no hardware — so it is
//! directly unit-testable against published known-answer vectors.

use aes::cipher::{Array, BlockCipherDecrypt, BlockCipherEncrypt, KeyInit as _};
use aes::Aes128;
use cmac::{Cmac, Mac};

use super::error::SamError;
use super::transport::SamTransport;

/// AES-128-CMAC per RFC 4493, over an arbitrary-length message.
///
/// This is the MAC primitive underlying both AN10922 key diversification and
/// (per this module's HARDWARE-VERIFY host KDF) `SAM_AuthenticateHost`
/// session-key derivation.
pub fn cmac_aes128(key: &[u8; 16], message: &[u8]) -> [u8; 16] {
    let mut mac = Cmac::<Aes128>::new_from_slice(key).expect("Aes128 key is always 16 bytes");
    Mac::update(&mut mac, message);
    let tag = mac.finalize().into_bytes();
    let mut out = [0u8; 16];
    out.copy_from_slice(&tag);
    out
}

/// AES-128-CMAC (RFC 4493) but starting the CBC chain from an arbitrary `iv`
/// instead of the all-zero block. With `iv = [0; 16]` this is *exactly*
/// [`cmac_aes128`]; a non-zero `iv` is what liblogicalaccess's
/// `CMACCrypto::cmac(key, cipher, data, IV, 16)` uses to chain one protected
/// command's MAC into the next via `d_lastMacIV` (see [`SecureMessaging`]).
/// Subkey derivation (`L = AES-ECB(K, 0^128)`, then `K1`/`K2`) is
/// IV-independent, exactly per RFC 4493 §2.3.
pub fn cmac_aes128_with_iv(key: &[u8; 16], iv: &[u8; 16], message: &[u8]) -> [u8; 16] {
    let cipher = Aes128::new(&Array::from(*key));
    let enc = |b: [u8; 16]| -> [u8; 16] {
        let mut ga = Array::from(b);
        cipher.encrypt_block(&mut ga);
        let mut o = [0u8; 16];
        o.copy_from_slice(&ga);
        o
    };

    // Subkeys (RFC 4493 §2.3).
    let l = enc([0u8; 16]);
    let k1 = cmac_subkey(&l);
    let k2 = cmac_subkey(&k1);

    // Last block (RFC 4493 §2.4): a full final block is XORed with K1; an
    // incomplete (or empty) final block is 10*-padded and XORed with K2.
    let n_full = message.len() / 16;
    let complete = !message.is_empty() && message.len() % 16 == 0;
    let mut last = [0u8; 16];
    if complete {
        let start = (n_full - 1) * 16;
        for i in 0..16 {
            last[i] = message[start + i] ^ k1[i];
        }
    } else {
        let rem = &message[n_full * 16..];
        last[..rem.len()].copy_from_slice(rem);
        last[rem.len()] = 0x80;
        for i in 0..16 {
            last[i] ^= k2[i];
        }
    }

    // CBC-MAC over all complete leading blocks, chain seeded from `iv`.
    let leading = if complete { n_full - 1 } else { n_full };
    let mut x = *iv;
    for b in 0..leading {
        let mut blk = [0u8; 16];
        for i in 0..16 {
            blk[i] = x[i] ^ message[b * 16 + i];
        }
        x = enc(blk);
    }
    let mut blk = [0u8; 16];
    for i in 0..16 {
        blk[i] = x[i] ^ last[i];
    }
    enc(blk)
}

/// One CMAC subkey-derivation step (RFC 4493 §2.3): left-shift the 16-byte
/// block by one bit; if the shifted-out high bit was set, XOR in the
/// block-size constant `Rb = 0x00…0087`.
fn cmac_subkey(input: &[u8; 16]) -> [u8; 16] {
    let msb_set = input[0] & 0x80 != 0;
    let mut out = [0u8; 16];
    let mut carry = 0u8;
    for i in (0..16).rev() {
        out[i] = (input[i] << 1) | carry;
        carry = (input[i] >> 7) & 1;
    }
    if msb_set {
        out[15] ^= 0x87;
    }
    out
}

/// liblogicalaccess `truncateMacBuffer` followed by "take the first 8 bytes":
/// the SAM's 8-byte MAC over a Full-protection command (and the host-auth
/// CMACs) is the **odd-indexed** bytes of the 16-byte AES-CMAC output —
/// bytes 1,3,5,…,15, in ascending index order.
pub fn truncate_mac_odd(full: &[u8; 16]) -> [u8; 8] {
    let mut out = [0u8; 8];
    for (i, b) in full.iter().enumerate() {
        if i % 2 != 0 {
            out[i / 2] = *b;
        }
    }
    out
}

/// AES-128-CBC encryption with **no padding** — `data` must already be a
/// multiple of 16 bytes; callers pad explicitly so the padding scheme stays
/// visible at the call site (see the `// HARDWARE-VERIFY` note on
/// [`super::apdu::build_change_key_entry_full_from_entry`], where the exact
/// SAM AV3 padding/IV convention is unconfirmed).
pub fn aes128_cbc_encrypt_no_pad(
    key: &[u8; 16],
    iv: &[u8; 16],
    data: &[u8],
) -> Result<Vec<u8>, SamError> {
    if data.is_empty() || !data.len().is_multiple_of(16) {
        return Err(SamError::Crypto(format!(
            "AES-CBC input must be a non-zero multiple of 16 bytes, got {}",
            data.len()
        )));
    }
    let cipher = Aes128::new(&Array::from(*key));
    let mut prev = *iv;
    let mut out = Vec::with_capacity(data.len());
    for chunk in data.chunks(16) {
        let mut block = [0u8; 16];
        for i in 0..16 {
            block[i] = chunk[i] ^ prev[i];
        }
        let mut ga = Array::from(block);
        cipher.encrypt_block(&mut ga);
        out.extend_from_slice(&ga);
        prev.copy_from_slice(&ga);
    }
    Ok(out)
}

/// AES-128-CBC decryption with **no padding removal** — `data` must already
/// be a multiple of 16 bytes; the caller strips any padding itself. Mirrors
/// [`aes128_cbc_encrypt_no_pad`] (same no-padding contract), needed by the
/// `SAM_AuthenticateHost` pass-2/pass-3 handshake to decrypt `EncKex(RndB)`.
pub fn aes128_cbc_decrypt_no_pad(
    key: &[u8; 16],
    iv: &[u8; 16],
    data: &[u8],
) -> Result<Vec<u8>, SamError> {
    if data.is_empty() || !data.len().is_multiple_of(16) {
        return Err(SamError::Crypto(format!(
            "AES-CBC input must be a non-zero multiple of 16 bytes, got {}",
            data.len()
        )));
    }
    let cipher = Aes128::new(&Array::from(*key));
    let mut prev = *iv;
    let mut out = Vec::with_capacity(data.len());
    for chunk in data.chunks(16) {
        let mut cipher_block = [0u8; 16];
        cipher_block.copy_from_slice(chunk);
        let mut ga = Array::from(cipher_block);
        cipher.decrypt_block(&mut ga);
        let mut plain = [0u8; 16];
        for i in 0..16 {
            plain[i] = ga[i] ^ prev[i];
        }
        out.extend_from_slice(&plain);
        prev = cipher_block; // CBC: this ciphertext block is the next IV.
    }
    Ok(out)
}

/// Left-rotate a 16-byte block by `n` bytes (`n` taken mod 16):
/// `data[n..] || data[..n]`. Used by the `SAM_AuthenticateHost` pass-3
/// challenge-response construction (`rotl(RndB, 2)`), aligned to the
/// `dumacp/smartcard` reference's `rotate()` helper.
pub fn rotate_left_16(data: &[u8; 16], n: usize) -> [u8; 16] {
    let n = n % 16;
    let mut out = [0u8; 16];
    out[..16 - n].copy_from_slice(&data[n..]);
    out[16 - n..].copy_from_slice(&data[..n]);
    out
}

/// Compute the `cmac2` field sent in `SAM_AuthenticateHost` pass 2: the
/// odd-indexed-byte truncation (bytes 1,3,5,...,15 — 8 of the 16 CMAC output
/// bytes, in ascending index order) of
/// `CMAC(hostMasterKey, RndB'[12] || hostMode || 00 00 00)`.
///
/// HARDWARE-VERIFY: this odd-byte truncation scheme is unconfirmed against
/// the DS3235 spec text directly; it is exactly what the `dumacp/smartcard`
/// reference (exercised against real AV2/AV3 hardware) does. See
/// [`derive_host_session_keys`] doc comment for the overall alignment
/// rationale.
pub fn host_auth_pass2_cmac(master_key: &[u8; 16], rnd2: &[u8; 12], host_mode: u8) -> [u8; 8] {
    let mut var1 = Vec::with_capacity(16);
    var1.extend_from_slice(rnd2);
    var1.push(host_mode);
    var1.extend_from_slice(&[0u8; 3]);
    let full = cmac_aes128(master_key, &var1);
    truncate_mac_odd(&full)
}

/// Derive the intermediate `Kex` session key used to decrypt `EncKex(RndB)`
/// in `SAM_AuthenticateHost` pass 2 and to encrypt the pass-3 challenge:
/// `Kex = AES-128-CBC-Encrypt(hostMasterKey, IV=0, DivKey)` where
/// `DivKey = rnd1[7..12] || rnd2[7..12] || (rnd1[0..5] XOR rnd2[0..5]) || 0x91`
/// (16 bytes: 5 + 5 + 5 + 1).
///
/// HARDWARE-VERIFY: aligned to `dumacp/smartcard` samav2 `AuthHostAV2`
/// (real-hardware-exercised reference); unconfirmed against DS3235 directly.
/// See [`derive_host_session_keys`].
pub fn derive_kex(
    master_key: &[u8; 16],
    rnd1: &[u8; 12],
    rnd2: &[u8; 12],
) -> Result<[u8; 16], SamError> {
    let mut div_key = Vec::with_capacity(16);
    div_key.extend_from_slice(&rnd1[7..12]);
    div_key.extend_from_slice(&rnd2[7..12]);
    for i in 0..5 {
        div_key.push(rnd1[i] ^ rnd2[i]);
    }
    div_key.push(0x91);
    let kex = aes128_cbc_encrypt_no_pad(master_key, &[0u8; 16], &div_key)?;
    let mut out = [0u8; 16];
    out.copy_from_slice(&kex);
    Ok(out)
}

/// AN10922 AES-128 key diversification: `DivKey = AES-128-CMAC(MasterKey, D)`
/// where `D = 0x01 || M` (the `0x01` algorithm-identifier byte is fixed for
/// AES-128; other byte lengths use a different fixed prefix per AN10922,
/// which this module does not implement — only AES-128 is in scope).
///
/// `M` (the diversification input) must be 1..=31 bytes so that `D` never
/// exceeds 32 bytes; per AN10922 this is required regardless of whether `D`
/// spans one or two CMAC blocks — `cmac_aes128` handles both cases
/// transparently (RFC 4493 already branches internally on whether the final
/// block is complete).
pub fn diversify_key_aes128(master_key: &[u8; 16], div_input: &[u8]) -> Result<[u8; 16], SamError> {
    if div_input.is_empty() || div_input.len() > 31 {
        return Err(SamError::Crypto(format!(
            "AN10922 diversification input M must be 1..=31 bytes, got {}",
            div_input.len()
        )));
    }
    let mut d = Vec::with_capacity(1 + div_input.len());
    d.push(0x01); // fixed algorithm-identifier byte for AES-128 (AN10922)
    d.extend_from_slice(div_input);
    Ok(cmac_aes128(master_key, &d))
}

/// Assemble the project's recommended AN10922 diversification input
/// `M = UID[7] || KeyNo || AID[3]` (11 bytes) — identical at personalization
/// time and at authentication time, per the architecture doc.
pub fn diversification_input(uid: &[u8; 7], key_no: u8, aid: &[u8; 3]) -> [u8; 11] {
    let mut m = [0u8; 11];
    m[0..7].copy_from_slice(uid);
    m[7] = key_no;
    m[8..11].copy_from_slice(aid);
    m
}

/// Session keys derived from `SAM_AuthenticateHost` (Ke = command-encryption
/// key, Km = command-MAC key), used to wrap `SAM_ChangeKeyEntry` and other
/// commands sent to the SAM in Full protection mode.
#[derive(Clone)]
pub struct HostSessionKeys {
    pub ke: [u8; 16],
    pub km: [u8; 16],
}

/// Redacted `Debug` — deliberately does **not** print `ke`/`km`. Only
/// needed so `Result<HostSessionKeys, _>::unwrap_err()` compiles in tests;
/// `sam_ceremony.rs` never logs a `HostSessionKeys` value.
impl std::fmt::Debug for HostSessionKeys {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("HostSessionKeys")
            .field("ke", &"<redacted>")
            .field("km", &"<redacted>")
            .finish()
    }
}

/// HARDWARE-VERIFY: derive the host-authentication session keys (Ke, Km)
/// from the long-term host master key and the two full 16-byte random
/// challenges (`RndA` generated by the host, `RndB` recovered by decrypting
/// pass 2's `EncKex(RndB)` under [`derive_kex`]'s `Kex`).
///
/// **Aligned to `github.com/dumacp/smartcard`'s `AuthHostAV2`** (package
/// `nxp/mifare/samav2`), an open-source Go SAM-AV2 client exercised against
/// real hardware — used here in preference to a from-scratch guess, per the
/// project's "a known-working reference beats a guess" policy for this bead
/// (3pn). This reportedly diverges from the NDA-restricted DS3235 spec text
/// (which we don't have unrestricted access to) — **still unconfirmed
/// against real SAM AV3 + DS3235**; do not trust in production before a
/// hardware bench test (see architecture doc Kap. 9).
///
/// `Km = AES-128-CBC-Encrypt(hostMasterKey, IV=0, SV2)` where
/// `SV2 = RndA[7..12] || RndB[7..12] || (RndB[0..5] XOR RndA[0..5]) || 0x82`.
///
/// `Ke = AES-128-CBC-Encrypt(hostMasterKey, IV=0, SV1)` where
/// `SV1 = RndA[11..16] || RndB[11..16] || (RndB[4..9] XOR RndA[4..9]) || 0x81`.
///
/// Both `SV1`/`SV2` are 16 bytes (5 + 5 + 5 + 1) and are keyed by the
/// **long-term host master key**, not `Kex` — `Kex` is only used to unwrap
/// `RndB` in pass 2 / wrap the pass-3 challenge, per [`derive_kex`].
pub fn derive_host_session_keys(
    master_key: &[u8; 16],
    rnd_a: &[u8; 16],
    rnd_b: &[u8; 16],
) -> Result<HostSessionKeys, SamError> {
    let mut sv2 = Vec::with_capacity(16);
    sv2.extend_from_slice(&rnd_a[7..12]);
    sv2.extend_from_slice(&rnd_b[7..12]);
    for i in 0..5 {
        sv2.push(rnd_b[i] ^ rnd_a[i]);
    }
    sv2.push(0x82);
    let km_vec = aes128_cbc_encrypt_no_pad(master_key, &[0u8; 16], &sv2)?;

    let mut sv1 = Vec::with_capacity(16);
    sv1.extend_from_slice(&rnd_a[11..16]);
    sv1.extend_from_slice(&rnd_b[11..16]);
    for i in 0..5 {
        sv1.push(rnd_b[4 + i] ^ rnd_a[4 + i]);
    }
    sv1.push(0x81);
    let ke_vec = aes128_cbc_encrypt_no_pad(master_key, &[0u8; 16], &sv1)?;

    let mut ke = [0u8; 16];
    ke.copy_from_slice(&ke_vec);
    let mut km = [0u8; 16];
    km.copy_from_slice(&km_vec);
    Ok(HostSessionKeys { ke, km })
}

/// Derive the **offline** change-key session keys `(Ke, Km)` for a
/// `SAM_ChangeKeyEntry` performed WITHOUT a host session — the re-key path
/// for a `KeyClass = OfflineChange` entry, whose session is derived from the
/// entry's OWN CURRENT KEY plus a 2-byte change counter, so it needs **no**
/// `SAM_AuthenticateHost` (which returns `6985` on this project's SAM AV3 —
/// bead 59m). Port of liblogicalaccess `generateOfflineSessionKey`
/// (`~/.zeiterfassung-ops/sam-refs/samav2iso7816commands.cpp` lines 135-203),
/// **AES-128 branch only** (`entry_key` is 16 bytes).
///
/// AES-128 branch (reference lines 164-169, 180/183):
/// - `SV1a = changecnt_be(2) || 0x71` repeated to 16 bytes
/// - `SV2a = changecnt_be(2) || 0x72` repeated to 16 bytes
/// - `Ke = AES-ECB(entry_key, SV1a)`, `Km = AES-ECB(entry_key, SV2a)`
///
/// The reference ciphers each 16-byte SV as a single block under `entry_key`
/// with an all-zero IV and no chaining (`cipher->cipher(SV, .., emptyIV,
/// false)`), i.e. plain AES-ECB of one block — which is exactly
/// [`aes128_cbc_encrypt_no_pad`] with `IV = 0` over one block. `SV1b`/`SV2b`
/// (the `0x00`-filled variants) are only used to extend the key for AES-192/256
/// and are unused here, so this returns the single-block `(Ke, Km)` directly.
///
/// `changecnt` is encoded **big-endian** (reference lines 146-147:
/// `(changecnt >> 8) & 0xff` then `changecnt & 0xff`).
pub fn generate_offline_session_key(
    entry_key: &[u8; 16],
    changecnt: u16,
) -> Result<([u8; 16], [u8; 16]), SamError> {
    let cc_hi = (changecnt >> 8) as u8;
    let cc_lo = (changecnt & 0xff) as u8;

    let mut sv1a = [0x71u8; 16];
    sv1a[0] = cc_hi;
    sv1a[1] = cc_lo;
    let mut sv2a = [0x72u8; 16];
    sv2a[0] = cc_hi;
    sv2a[1] = cc_lo;

    let ke_vec = aes128_cbc_encrypt_no_pad(entry_key, &[0u8; 16], &sv1a)?;
    let km_vec = aes128_cbc_encrypt_no_pad(entry_key, &[0u8; 16], &sv2a)?;

    let mut ke = [0u8; 16];
    ke.copy_from_slice(&ke_vec);
    let mut km = [0u8; 16];
    km.copy_from_slice(&km_vec);
    Ok((ke, km))
}

/// Full-protection secure-messaging session state for host-mediated SAM AV3
/// commands, ported byte-for-byte from liblogicalaccess
/// `SAMAV2ISO7816Commands` (`createfullProtectionCmd` + `generateEncIV`, see
/// `~/.zeiterfassung-ops/sam-refs/samav2iso7816commands.cpp`).
///
/// Holds the enc key `ke` (liblo `d_sessionKey`), MAC key `km`
/// (`d_macSessionKey`), the command counter (`d_cmdCtr`), and the two chained
/// IVs (`d_LastSessionIV` / `d_lastMacIV`). Each command wrapped via
/// [`create_full_protection_cmd`](Self::create_full_protection_cmd) advances
/// both IV chains exactly as the reference does.
///
/// **HARDWARE-VERIFY (starting state).** A freshly [`new`](Self::new)
/// session has `cmd_ctr = 0` and both IV seeds all-zero. This mirrors the
/// state liblo's `authenticateHost` leaves behind — `d_lastMacIV =
/// d_LastSessionIV = emptyIV(16)` and `d_cmdCtr = 0` (reference lines
/// 217-218, 331). Our SAM AV3's `SAM_LockUnlock`/Unlock is itself the host
/// authentication (bead 59m), so the `SAM_ChangeKeyEntry` that follows it
/// runs on a session with this same all-zero seed and counter 0. liblo's own
/// `lockUnlock` never establishes a Full-protection session (it only derives
/// `d_authKey`), so there is no *direct* reference for the post-LockUnlock SM
/// seed — the all-zero/`ctr=0` choice is the most reference-faithful analog
/// (identical to `authenticateHost`'s ending state). CONFIRMED on hardware:
/// the `SAM_ChangeKeyEntry` (bead 59m) and `SAM_DumpSecretKey` (bead eyc)
/// Full-protection exchanges both succeed with this seed.
pub struct SecureMessaging {
    pub ke: [u8; 16],
    pub km: [u8; 16],
    pub cmd_ctr: u32,
    pub last_session_iv: [u8; 16],
    pub last_mac_iv: [u8; 16],
}

impl SecureMessaging {
    /// Seed a session directly from the host-authentication result. Matches
    /// the post-handshake state (see the type-level HARDWARE-VERIFY note):
    /// `cmd_ctr = 0`, both IV chains all-zero.
    pub fn new(ke: [u8; 16], km: [u8; 16]) -> Self {
        Self {
            ke,
            km,
            cmd_ctr: 0,
            last_session_iv: [0u8; 16],
            last_mac_iv: [0u8; 16],
        }
    }

    /// The command counter as a 4-byte **big-endian** value.
    ///
    /// This is where the AV3 SAM deviates from the liblo reference. liblo's
    /// `BufferHelper::setUInt32` writes big-endian and then `reverse()`s it to
    /// little-endian; porting that faithfully verified the request MAC (which
    /// runs at `CmdCtr = 0`, where BE ≡ LE) but the response MAC (`CmdCtr = 1`)
    /// never verified. An offline exhaustive search over a captured
    /// `(Km, encData, SW → MAC8)` vector (bead eyc, 2026-07-15) pinned the
    /// construction to a **4-byte big-endian** counter — i.e. liblo's value
    /// BEFORE the `reverse`. See `verify_and_decrypt_response`'s regression
    /// test `response_mac_matches_captured_av3_hardware_vector`.
    fn cmd_ctr_be(&self) -> [u8; 4] {
        self.cmd_ctr.to_be_bytes()
    }

    /// Port of liblo `generateEncIV`: build
    /// `myIV = [fill; 4] || ctr || ctr || ctr` (16 bytes; `fill = 0x01` for the
    /// encrypt IV, `0x02` for the decrypt IV, and `ctr` is
    /// [`cmd_ctr_be`](Self::cmd_ctr_be) — big-endian, per the AV3 finding, not
    /// liblo's reversed LE), then `AES-128-CBC(ke, IV = last_session_iv, myIV)`.
    /// Note liblo keys this with `d_sessionKey` (our `ke`), not the MAC key, and
    /// uses the *current* `d_LastSessionIV` as the CBC IV (all-zero for the
    /// first command's request, and reset to zero for its response).
    ///
    /// HARDWARE-VERIFY: only the MAC's counter encoding was pinned empirically
    /// (offline vector). The decrypt IV reuses that same big-endian `CmdCtr`
    /// for consistency; the recovered 16-byte key is confirmed end-to-end when
    /// the software ChangeKey it feeds succeeds on the card.
    fn generate_enc_iv(&self, encrypt: bool) -> Result<[u8; 16], SamError> {
        let ctr_be = self.cmd_ctr_be();
        let fill = if encrypt { 0x01u8 } else { 0x02u8 };
        let mut my_iv = Vec::with_capacity(16);
        my_iv.extend_from_slice(&[fill; 4]);
        for _ in 1..4 {
            my_iv.extend_from_slice(&ctr_be);
        }
        let enc = aes128_cbc_encrypt_no_pad(&self.ke, &self.last_session_iv, &my_iv)?;
        let mut out = [0u8; 16];
        out.copy_from_slice(&enc);
        Ok(out)
    }

    /// Wrap a plain SAM command APDU into a Full-protection command,
    /// byte-for-byte per liblo `createfullProtectionCmd`. Two APDU shapes are
    /// supported (fail-closed on anything else):
    ///
    /// - `CLA INS P1 P2 LC DATA` (`lc && !le`) — the case-3 shape
    ///   `SAM_ChangeKeyEntry` uses (bead 59m, hardware-confirmed).
    /// - `CLA INS P1 P2 LC DATA LE` (`lc && le`) — the case-4 shape
    ///   `SAM_DumpSecretKey` uses (bead eyc). Per the reference, the trailing
    ///   `Le` byte is carried — unencrypted — into BOTH the CMAC input
    ///   (`protectedCmd`, at the very end after `encData`) AND the final wire
    ///   command AFTER the 8-byte MAC (`finalFullProtectedCmd`). It is a
    ///   data-returning command's request-for-response marker; without it the
    ///   SAM returns `9000` but no key (empirically observed, 2026-07-15).
    ///
    /// The `!lc` (case-1/case-2, no data field) shapes the reference also
    /// handles are not needed by this project and remain unsupported.
    ///
    /// Advances both IV chains: `last_session_iv` becomes the per-command enc
    /// IV; `last_mac_iv` becomes the last CBC block of the MAC input's
    /// complete-block prefix (the reference stores the *prefix* CBC block, not
    /// the final CMAC, into `d_lastMacIV`). Does **not** touch `cmd_ctr` —
    /// liblo increments it in `transmit`, after the wrap; for the single
    /// ceremony ChangeKeyEntry it stays 0.
    pub fn create_full_protection_cmd(&mut self, cmd: &[u8]) -> Result<Vec<u8>, SamError> {
        // liblo AV2_HEADER_LENGTH (CLA INS P1 P2 LC) and AV2_LC_POS.
        const HDR: usize = 5;
        const LC_POS: usize = 4;

        if cmd.len() < HDR {
            return Err(SamError::Crypto(format!(
                "createfullProtectionCmd: command too short ({} bytes)",
                cmd.len()
            )));
        }
        let lcvalue = cmd[LC_POS] as usize;
        // Accept "CLA INS P1 P2 LC DATA" (lc, !le) and "CLA INS P1 P2 LC DATA
        // LE" (lc, le). `le_byte` is `Some(Le)` for the case-4 shape.
        let le_byte = if lcvalue != 0 && HDR + lcvalue == cmd.len() {
            None
        } else if lcvalue != 0 && HDR + lcvalue + 1 == cmd.len() {
            Some(cmd[HDR + lcvalue])
        } else {
            return Err(SamError::Crypto(
                "createfullProtectionCmd: only the LC+DATA and LC+DATA+LE APDU shapes are \
                 supported"
                    .into(),
            ));
        };

        let data_plain = &cmd[HDR..HDR + lcvalue];
        let header = &cmd[0..HDR]; // CLA INS P1 P2 LC(orig)

        // ISO/IEC 9797-1 method-2 padding: append 0x80 then zeros to the next
        // 16-byte boundary — but ONLY when the data is not already block
        // aligned (liblo adds no padding to an already-aligned field).
        let mut data = data_plain.to_vec();
        if data.len() % 16 != 0 {
            data.push(0x80);
            while data.len() % 16 != 0 {
                data.push(0x00);
            }
        }

        // Per-command enc IV, then AES-CBC-encrypt the padded data under Ke.
        self.last_session_iv = self.generate_enc_iv(true)?;
        let enc_data = aes128_cbc_encrypt_no_pad(&self.ke, &self.last_session_iv, &data)?;

        // Lc fixup: encData length + 8-byte MAC (liblo `encData.size() + 8`).
        let lc_fixed = (enc_data.len() + 8) as u8;

        // CMAC input ("protectedCmd"): header + encData (+ Le for the case-4
        // shape, at the very end after encData — byte-exact to liblo, which
        // leaves the trailing Le on `protectedCmd`), Lc fixed up, with the
        // 4-byte little-endian counter inserted at offset 2. The counter is in
        // the MAC input ONLY — never in the wire command.
        let mut protected = Vec::with_capacity(HDR + enc_data.len() + 5);
        protected.extend_from_slice(header);
        protected.extend_from_slice(&enc_data);
        if let Some(le) = le_byte {
            protected.push(le);
        }
        protected[LC_POS] = lc_fixed;
        protected.splice(2..2, self.cmd_ctr_be().iter().copied());

        // Wire command ("finalFullProtectedCmd"): header + encData, Lc fixed
        // up, NO counter, MAC8 appended after encData, then (case-4 only) the
        // trailing Le AFTER the MAC — byte-exact to liblo's
        // finalFullProtectedCmd. ChangeKeyEntry (case 3) stays Le-less and is
        // HARDWARE-CONFIRMED (a spurious trailing Le -> 6A84, bead 59m);
        // DumpSecretKey (case 4) keeps its Le so the SAM emits the key (bead
        // eyc).
        let mut wire = Vec::with_capacity(HDR + enc_data.len() + 8 + 1);
        wire.extend_from_slice(header);
        wire.extend_from_slice(&enc_data);
        wire[LC_POS] = lc_fixed;

        // MAC: CBC the complete-block prefix under Km (IV = last_mac_iv), set
        // last_mac_iv to that CBC's final block, then CMAC the remaining
        // (always incomplete) tail under Km with the updated IV — exactly
        // liblo's split. The tail length is `protected.len() % 16` (0..15).
        let block_ready = (protected.len() / 16) * 16;
        let mac8 = if block_ready >= 16 {
            let prefix = &protected[..block_ready];
            let cbc = aes128_cbc_encrypt_no_pad(&self.km, &self.last_mac_iv, prefix)?;
            self.last_mac_iv.copy_from_slice(&cbc[cbc.len() - 16..]);
            let tail = &protected[block_ready..];
            truncate_mac_odd(&cmac_aes128_with_iv(&self.km, &self.last_mac_iv, tail))
        } else {
            // No complete leading block — never happens for ChangeKeyEntry,
            // but stay faithful: CMAC the whole thing under the current IV.
            truncate_mac_odd(&cmac_aes128_with_iv(
                &self.km,
                &self.last_mac_iv,
                &protected,
            ))
        };
        wire.extend_from_slice(&mac8);
        // Case-4 (DumpSecretKey): the trailing Le rides on the wire AFTER the
        // MAC — liblo inserts it at `AV2_HEADER_LENGTH + encData.size()` on a
        // `finalFullProtectedCmd` that still carried its original Le. Case-3
        // (ChangeKeyEntry): no Le, wire ends at the MAC (spurious Le -> 6A84,
        // HARDWARE-CONFIRMED, bead 59m).
        if let Some(le) = le_byte {
            wire.push(le);
        }

        Ok(wire)
    }

    /// Verify+decrypt the SAM's response to a Full-protection command sent
    /// via [`Self::create_full_protection_cmd`], assuming the caller has
    /// ALREADY applied the exact state transitions `transmit()` performs
    /// around a ONE-SHOT (`first=true,last=true`) full-protection exchange —
    /// use [`Self::full_protection_round_trip`] rather than calling this
    /// directly. Port of liblogicalaccess `verifyAndDecryptResponse`
    /// (`~/.zeiterfassung-ops/sam-refs/samav2iso7816commands.cpp` lines
    /// 454-527).
    ///
    /// Response wire format: `<data...> <MAC[8]> <SW[2]>` (data may be
    /// empty — a bare `<MAC[8]><SW[2]>`, 10 bytes total). Fail-closed: any
    /// MAC mismatch is a hard [`SamError::CmacMismatch`] — never returns
    /// unverified data.
    ///
    /// HARDWARE-VERIFY (bundled, all unconfirmed against real hardware —
    /// this is the first caller in this codebase to exercise ANY SAM
    /// Full-protection RESPONSE verification/decryption; only the
    /// request-wrapping half has hardware-adjacent confidence so far via
    /// `SAM_ChangeKeyEntry`, bead 59m):
    /// - The MAC input is `SW[2] || CmdCtr[4,LE] || <data if any>`, split
    ///   into a CBC-MAC'd complete-block prefix (chaining `last_mac_iv`
    ///   exactly like the request side) plus a CMAC'd tail — byte-for-byte
    ///   the reference's construction.
    /// - Decryption (when data is present) uses
    ///   [`Self::generate_enc_iv`]`(encrypt=false)` — the same per-command
    ///   IV formula as the request side, just with the decrypt fill byte.
    /// - **Unpadding is a literal, bug-compatible port of the reference's
    ///   loop**: it strips trailing `0x00` bytes then one `0x80` marker,
    ///   but ALSO drops one byte when the plaintext was never padded to
    ///   begin with (its last byte is neither `0x00` nor `0x80`) — see the
    ///   inline comment below. This means a response whose plaintext
    ///   happens to be EXACTLY block-aligned (e.g. `SAM_DumpSecretKey`'s
    ///   16-byte diversified key, if the SAM does not itself pad an
    ///   already-aligned response) will come back ONE BYTE SHORT. If the
    ///   diversified key reads back as 15 bytes instead of 16 on the
    ///   bench, this is the first place to check — the fix is to detect
    ///   the "known-length response, no padding marker present" case and
    ///   skip the truncation, but that requires the caller to tell this
    ///   function the expected plaintext length, which it does not
    ///   currently accept (kept byte-exact to the reference instead of
    ///   guessing a fix with no hardware to confirm it against).
    pub fn verify_and_decrypt_response(&mut self, response: &[u8]) -> Result<Vec<u8>, SamError> {
        if response.len() < 10 {
            // Too short to carry an 8-byte MAC + 2-byte status word — liblo
            // returns the response unchanged rather than erroring here (the
            // caller's own status-word check downstream is expected to
            // reject it); mirrored as-is.
            return Ok(response.to_vec());
        }
        let mac = &response[response.len() - 10..response.len() - 2];
        let sw = &response[response.len() - 2..];
        let has_data = response.len() != 10;

        // AV3 response-MAC input: SW[2] || CmdCtr[4, BIG-ENDIAN] || encData.
        // The big-endian counter (not liblo's reversed LE) is the pinned AV3
        // deviation — see `cmd_ctr_be` and the regression test.
        let mut mac_input = Vec::with_capacity(response.len());
        mac_input.extend_from_slice(sw);
        mac_input.extend_from_slice(&self.cmd_ctr_be());
        if has_data {
            mac_input.extend_from_slice(&response[..response.len() - 10]);
        }

        let block_ready = (mac_input.len() / 16) * 16;
        let computed_mac = if block_ready > 0 {
            let prefix = &mac_input[..block_ready];
            let cbc = aes128_cbc_encrypt_no_pad(&self.km, &self.last_mac_iv, prefix)?;
            self.last_mac_iv.copy_from_slice(&cbc[cbc.len() - 16..]);
            let tail = &mac_input[block_ready..];
            truncate_mac_odd(&cmac_aes128_with_iv(&self.km, &self.last_mac_iv, tail))
        } else {
            truncate_mac_odd(&cmac_aes128_with_iv(
                &self.km,
                &self.last_mac_iv,
                &mac_input,
            ))
        };

        if computed_mac != mac {
            return Err(SamError::CmacMismatch(
                "SAM Full-protection response MAC verification failed — refusing to trust this \
                 response"
                    .to_string(),
            ));
        }

        let mut data = if has_data {
            let enc_data = &response[..response.len() - 10];
            self.last_session_iv = self.generate_enc_iv(false)?;
            let dec = aes128_cbc_decrypt_no_pad(&self.ke, &self.last_session_iv, enc_data)?;

            // ISO/IEC 9797-1 method-2 unpad: walk back over trailing 0x00,
            // then strip the 0x80 marker if present. If there is no 0x80
            // (the plaintext was already block-aligned with no pad, e.g. a
            // 16-byte DumpSecretKey result), return it unchanged — do NOT
            // drop a data byte.
            let mut i = dec.len();
            while i > 0 && dec[i - 1] == 0x00 {
                i -= 1;
            }
            if i > 0 && dec[i - 1] == 0x80 {
                dec[..i - 1].to_vec()
            } else {
                dec
            }
        } else {
            Vec::new()
        };
        data.extend_from_slice(sw);
        Ok(data)
    }

    /// Perform one ONE-SHOT (`first=true,last=true`) Full-protection round
    /// trip: wrap `plain_cmd` ([`Self::create_full_protection_cmd`]), send it
    /// via `sam`, then verify+decrypt the response
    /// ([`Self::verify_and_decrypt_response`]). Used by
    /// [`super::flows::dump_secret_key`] for `SAM_DumpSecretKey`.
    ///
    /// **IV/counter ordering — matches the liblo reference `transmit()`
    /// (lines 559-599), now confirmed against real AV3 hardware (bead eyc,
    /// 2026-07-15).** For a one-shot exchange `transmit()`: (1) builds the
    /// request (leaving the request's IV state), (2) resets BOTH IV chains to
    /// zero, (3) increments `CmdCtr` (0 → 1), then (4) verifies the response.
    /// So the response MAC/decrypt run at `CmdCtr = 1` with zero-seeded IVs.
    ///
    /// The one place the wire scheme still deviates from liblo is the counter
    /// *encoding* (big-endian, see [`Self::cmd_ctr_be`]) — invisible here at
    /// `CmdCtr = 0` on the request, decisive at `CmdCtr = 1` on the response.
    /// The empirical hunt that established this (reset-IV+ctr0, then
    /// chained-IV+ctr0, both `CmacMismatch`) is retained only in the git
    /// history + the regression test; the final construction is
    /// `truncate_odd(CMAC(Km, IV=0, SW || CmdCtr_BE || encData))`.
    pub fn full_protection_round_trip(
        &mut self,
        sam: &mut dyn SamTransport,
        plain_cmd: &[u8],
    ) -> Result<Vec<u8>, SamError> {
        let wire = self.create_full_protection_cmd(plain_cmd)?;
        let resp = sam.transmit(&wire)?;
        // liblo `transmit()`, first=true: reset both IV chains to zero, then
        // bump the counter (0 → 1), BEFORE verifying the response.
        self.last_session_iv = [0u8; 16];
        self.last_mac_iv = [0u8; 16];
        self.cmd_ctr = self.cmd_ctr.wrapping_add(1);
        self.verify_and_decrypt_response(&resp)
    }
}

// ---------------------------------------------------------------------
// DESFire EV2 (card-side, software) crypto — bead 4ty rework 2026-07-15:
// personalization now authenticates to the freshly-created app's factory
// all-zero AES key ENTIRELY IN SOFTWARE (no SAM relay — see the
// `personalize` module-level "Incident 2026-07-15" doc comment for why
// `SAM_AuthenticatePICC` was abandoned) and diversifies+writes the app key
// via `SAM_DumpSecretKey` (get the raw diversified key from the SAM) +
// software `ChangeKey`, rather than `SAM_ChangeKeyPICC`.
//
// HARDWARE-PROVEN (bead eyc, 2026-07-15): the constructions below were
// reverse-engineered from the community/open-source DESFire EV2
// `AuthenticateEV2First` + secure-messaging scheme (NOT the restricted NXP
// datasheet, to which this project has no access) and cross-checked against
// hardware-proven references (libfreefare, nfcjlib, and
// `MichaelsPlayground/TalkToYourDESFireCard` `DesfireEv3.java`). The full
// offline chain — software EV2 auth, self-`ChangeKey` (no in-cryptogram CRC),
// re-auth with the diversified key, CommMode.MAC `CreateStdDataFile`, and
// CommMode.Full `WriteData` — now runs end-to-end on a real MF4SAM3 + DESFire
// EV2 fob. Card-side `CmdCtr` is little-endian (`DesfireEv3.java`
// `commandCounterLsb1`), distinct from the SAM AV3's big-endian counter.
// ---------------------------------------------------------------------

/// CRC-32/ISO-HDLC ("zlib"/gzip/PNG parameters — poly `0xEDB88320`
/// (reflected), init `0xFFFFFFFF`, reflected in/out, xorout `0xFFFFFFFF`),
/// appended little-endian. This is the CRC32 variant commonly published for
/// DESFire's AES-family cryptogram construction (e.g. `ChangeKey`) —
/// HARDWARE-VERIFY, not confirmed against this project's own cards or the
/// restricted datasheet. Hand-rolled (bit-at-a-time) rather than pulling in
/// a crate dependency: only this one variant is needed. This is JAMCRC
/// (CRC-32 with xorout 0x00000000, NOT the inverted ISO-HDLC/zlib CRC); the
/// JAMCRC test vector `CRC32("123456789") == 0x340BC6D9` pins it below.
pub fn crc32_desfire(data: &[u8]) -> u32 {
    let mut crc = 0xFFFF_FFFFu32;
    for &byte in data {
        crc ^= u32::from(byte);
        for _ in 0..8 {
            let mask = (crc & 1).wrapping_neg();
            crc = (crc >> 1) ^ (0xEDB8_8320 & mask);
        }
    }
    // DESFire in-cryptogram CRC32 is JAMCRC: reflected poly 0xEDB88320,
    // init 0xFFFFFFFF, refin/refout, xorout 0x00000000 (NO final inversion).
    // This gives the residue-zero self-check the card firmware validates.
    // (Standard CRC-32/ISO-HDLC would invert here — that is a DIFFERENT CRC
    // and makes the card reject ChangeKey with an integrity error.)
    crc
}

/// Build the UNPADDED AES `ChangeKey` cryptogram plaintext for a SELF key
/// change (changing the same key number the session authenticated with — this
/// project's convention: single-key apps, `PersonalizeConfig::app_key_no`):
///
/// `NewKey[16] || KeyVersion[1]` (17 bytes) — **no in-cryptogram CRC32**.
///
/// **DESFire EV2 self-change carries NO CRC in the cryptogram** — integrity is
/// provided by the outer EV2 command CMAC (`CommMode.Full` CMACt). This is
/// byte-for-byte the `else` (same-key) branch of a hardware-proven EV2/EV3
/// reference (`MichaelsPlayground/TalkToYourDESFireCard` `DesfireEv3.java`
/// `changeKey…`): `data = keyNew || KEY_VERSION || 0x80-pad`. Only the OTHER,
/// cross-key branch there builds `(NewKey XOR OldKey) || KeyVer ||
/// CRC32(NewKey) || 0x80-pad` — a CRC our flow must NOT use.
///
/// The caller pads (ISO/IEC 9797-1 method-2, `0x80 || 00…`) and encrypts via
/// [`DesfireEv2Session::encrypt_full`], which is exactly the reference's
/// `AES.encrypt(ivForCmdData, SesAuthENCKey, data-with-0x80-pad)`.
///
/// **History (bead eyc, 2026-07-15):** earlier revisions embedded a CRC32
/// (over `NewKey||KeyVer`, then over `Cmd||KeyNo||NewKey||KeyVer`) with
/// zero-padding — both drew `0x1E` INTEGRITY_ERROR from the card, because a
/// self-change must not carry a CRC. libfreefare/nfcjlib use a CRC, but those
/// are EV1 (legacy/ISO) secure messaging; under AuthenticateEV2First the CMACt
/// replaces the in-cryptogram CRC for the same-key case.
pub fn build_change_key_self_cryptogram(new_key: &[u8; 16], key_version: u8) -> Vec<u8> {
    let mut plain = Vec::with_capacity(17);
    plain.extend_from_slice(new_key);
    plain.push(key_version);
    plain
}

/// HARDWARE-VERIFY: `AuthenticateEV2First` session-key derivation
/// (`SesAuthENCKey`/`SesAuthMACKey`) — see the module-level note above.
/// `SV1`/`SV2` are each 32 bytes (2 AES blocks); the full 16-byte
/// `AES-128-CMAC` output is used directly as the session key — UNLIKE the
/// SAM's own MAC, no odd-byte truncation is applied here.
///
/// `Common = RndA[0..2] || (RndA[2..8] XOR RndB[0..6]) || RndB[6..16] ||
///           RndA[8..16]` (26 bytes)
/// `SV1 = A5 5A 00 01 00 80 || Common` (encryption session key)
/// `SV2 = 5A A5 00 01 00 80 || Common` (MAC session key)
pub fn derive_desfire_ev2_session_keys(
    key: &[u8; 16],
    rnd_a: &[u8; 16],
    rnd_b: &[u8; 16],
) -> ([u8; 16], [u8; 16]) {
    let mut common = Vec::with_capacity(26);
    common.extend_from_slice(&rnd_a[0..2]);
    for i in 0..6 {
        common.push(rnd_a[2 + i] ^ rnd_b[i]);
    }
    common.extend_from_slice(&rnd_b[6..16]);
    common.extend_from_slice(&rnd_a[8..16]);

    let mut sv1 = vec![0xA5, 0x5A, 0x00, 0x01, 0x00, 0x80];
    sv1.extend_from_slice(&common);
    let mut sv2 = vec![0x5A, 0xA5, 0x00, 0x01, 0x00, 0x80];
    sv2.extend_from_slice(&common);

    (cmac_aes128(key, &sv1), cmac_aes128(key, &sv2))
}

/// Software-side DESFire EV2 secure-messaging session, established by
/// [`super::flows::authenticate_picc_ev2_software`] (bead 4ty rework). Used
/// to build the `ChangeKey`/`WriteData` `CommMode.Full` cryptograms sent
/// directly to the card — see the module-level HARDWARE-VERIFY note.
///
/// Distinct from [`SecureMessaging`] (the SAM AV3's own, unrelated
/// proprietary host-command wrapping scheme) — same *shape* (enc key + MAC
/// key + a per-command IV derived from a counter), completely different
/// KDF/IV formulas. Do not conflate the two.
pub struct DesfireEv2Session {
    pub enc_key: [u8; 16],
    pub mac_key: [u8; 16],
    /// Transaction Identifier the card returned in `AuthenticateEV2First`'s
    /// final response. HARDWARE-VERIFY: this project's parser for that
    /// response ([`super::apdu::parse_picc_frame_pd_resp`]) returns the
    /// full 32-byte plaintext (`TI || RndA' || PDcap2 || PCDcap2`)
    /// undifferentiated — [`super::flows::authenticate_picc_ev2_software`]
    /// extracts `ti` as the first 4 bytes, matching the commonly-published
    /// field order, but this is unconfirmed.
    pub ti: [u8; 4],
    /// Command counter — starts at 0 right after authentication, advances
    /// by one after every command sent in this session (HARDWARE-VERIFY:
    /// including Plain-mode commands, not just Full/MAC ones — assumed
    /// here for IV/MAC continuity, not independently confirmed).
    pub cmd_ctr: u16,
}

/// Redacted `Debug` — deliberately does **not** print `enc_key`/`mac_key`,
/// mirroring [`HostSessionKeys`]'s same convention. Only needed so
/// `Result<Ev2SoftwareAuthResult, _>::unwrap_err()` compiles in
/// `flows.rs`'s tests; production code never logs a `DesfireEv2Session`.
impl std::fmt::Debug for DesfireEv2Session {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("DesfireEv2Session")
            .field("enc_key", &"<redacted>")
            .field("mac_key", &"<redacted>")
            .field("ti", &self.ti)
            .field("cmd_ctr", &self.cmd_ctr)
            .finish()
    }
}

impl DesfireEv2Session {
    pub fn new(enc_key: [u8; 16], mac_key: [u8; 16], ti: [u8; 4]) -> Self {
        Self {
            enc_key,
            mac_key,
            ti,
            cmd_ctr: 0,
        }
    }

    fn iv_block(&self, prefix: [u8; 2]) -> Result<[u8; 16], SamError> {
        let mut block = Vec::with_capacity(16);
        block.extend_from_slice(&prefix);
        block.extend_from_slice(&self.ti);
        block.extend_from_slice(&self.cmd_ctr.to_le_bytes());
        block.extend_from_slice(&[0u8; 8]);
        let enc = aes128_cbc_encrypt_no_pad(&self.enc_key, &[0u8; 16], &block)?;
        let mut out = [0u8; 16];
        out.copy_from_slice(&enc);
        Ok(out)
    }

    /// IV for encrypting this command's `CommMode.Full` data (PCD -> PICC).
    /// HARDWARE-VERIFY: the `0xA5 0x5A` prefix is chosen by analogy with the
    /// SAM's own encrypt/decrypt fill-byte convention
    /// ([`SecureMessaging::generate_enc_iv`]) and the SV1/SV2 prefixes
    /// above — not independently confirmed.
    pub fn command_iv(&self) -> Result<[u8; 16], SamError> {
        self.iv_block([0xA5, 0x5A])
    }

    /// IV for decrypting this command's response data (PICC -> PCD). Used by
    /// [`Self::decrypt_full`] — the daily punch flow (`punch::authenticate_and_read_token_id`,
    /// bead bi0 rework 2026-07-15) is this codebase's first CommMode.Full
    /// **response** consumer; `personalize::personalize_fob` only ever
    /// writes, so it never needed this (see that function's doc comment on
    /// the response-verification gap it explicitly accepts — this read path
    /// does NOT repeat that gap, see [`Self::decrypt_full`]/[`Self::mac_response`]).
    /// CONFIRMED on real hardware 2026-07-15 (zeit-terminal-01, bead 3l1, via
    /// an in-process `PUNCH_MAC_DIAG` brute-force against a real card): the
    /// `0x5A 0xA5` prefix is correct, and — critically — `self.cmd_ctr` must
    /// already be `N+1` here (the card increments its own counter BETWEEN
    /// command and response), not the command's own `N`. See
    /// [`super::flows::read_token_id_software`]'s single `advance()` call,
    /// placed right after the card accepts the command and before this IV
    /// is computed.
    pub fn response_iv(&self) -> Result<[u8; 16], SamError> {
        self.iv_block([0x5A, 0xA5])
    }

    /// Encrypt `plain_data` under `CommMode.Full`: ISO/IEC 9797-1 method-2
    /// pad (ALWAYS append `0x80` + zero-pad to the next 16-byte boundary,
    /// even if already aligned — HARDWARE-VERIFY: this project's SAM-side
    /// [`SecureMessaging`] only pads when NOT already aligned; DESFire's own
    /// EV2 convention is commonly published as padding unconditionally. If
    /// the card rejects this with a MAC/length error, try the SAM's
    /// aligned-means-no-pad rule instead), then AES-128-CBC-encrypt under
    /// [`Self::command_iv`].
    pub fn encrypt_full(&self, plain_data: &[u8]) -> Result<Vec<u8>, SamError> {
        let mut padded = plain_data.to_vec();
        padded.push(0x80);
        while padded.len() % 16 != 0 {
            padded.push(0x00);
        }
        let iv = self.command_iv()?;
        aes128_cbc_encrypt_no_pad(&self.enc_key, &iv, &padded)
    }

    /// EV2 `CommMode.Full` command MAC, truncated to the odd-indexed 8 bytes.
    /// MAC input (byte-for-byte per liblo `DESFireEV2Crypto::generateMAC`,
    /// `desfireev2crypto.cpp` lines 140-159):
    ///
    /// `Cmd || CmdCtr[2, LE] || TI[4] || CmdHeader || EncData`
    ///
    /// `cmd_and_header` is the plaintext command header the wire carries
    /// (`[Cmd, ...header]`, e.g. `[0xC4, KeyNo]` for ChangeKey). The **command
    /// byte comes FIRST**, before `CmdCtr` — an earlier revision placed the
    /// whole `cmd_and_header` after `CmdCtr || TI`, mis-MAC'ing every EV2 full
    /// command and drawing `0x1E` INTEGRITY_ERROR (bead eyc, 2026-07-15).
    ///
    /// `CmdCtr` is little-endian — CONFIRMED on hardware: `WriteData` at
    /// `CmdCtr = 1` succeeds with this encoding (bead eyc). The card is
    /// little-endian (`DesfireEv3.java` `commandCounterLsb1` /
    /// `intTo2ByteArrayInversed`), UNLIKE the SAM AV3's big-endian counter —
    /// the two are independent secure-messaging schemes, do not conflate them.
    pub fn mac_command(&self, cmd_and_header: &[u8], enc_data: &[u8]) -> [u8; 8] {
        let (cmd, header) = cmd_and_header
            .split_first()
            .expect("cmd_and_header must contain at least the command byte");
        let mut input = Vec::with_capacity(1 + 2 + 4 + header.len() + enc_data.len());
        input.push(*cmd);
        input.extend_from_slice(&self.cmd_ctr.to_le_bytes());
        input.extend_from_slice(&self.ti);
        input.extend_from_slice(header);
        input.extend_from_slice(enc_data);
        truncate_mac_odd(&cmac_aes128(&self.mac_key, &input))
    }

    /// Advance the command counter after a command completes — see the
    /// field doc HARDWARE-VERIFY.
    pub fn advance(&mut self) {
        self.cmd_ctr = self.cmd_ctr.wrapping_add(1);
    }

    /// Decrypt a `CommMode.Full` response's encrypted data field (PICC ->
    /// PCD), under [`Self::response_iv`], and strip the ISO/IEC 9797-1
    /// method-2 padding [`Self::encrypt_full`] unconditionally applies on the
    /// write side. Bead bi0 rework 2026-07-15 (`punch::authenticate_and_read_token_id`'s
    /// `ReadData`) is this codebase's first caller. CONFIRMED on real
    /// hardware 2026-07-15 (zeit-terminal-01, bead 3l1, via an in-process
    /// `PUNCH_MAC_DIAG` brute-force against a real card): this construction
    /// (`CmdCtr = N+1` via [`Self::response_iv`], `0x5A 0xA5` IV prefix)
    /// matches the card — the request-encryption half was already HW-proven
    /// separately via `ChangeKey`/`WriteData`.
    ///
    /// Unlike [`SecureMessaging::verify_and_decrypt_response`]'s bug-compatible
    /// unpad (which must special-case an already-aligned plaintext because the
    /// SAM's own request-side padding is conditional), this unpad has no such
    /// special case: [`Self::encrypt_full`] pads *unconditionally*, so a
    /// well-formed EV2 `CommMode.Full` response ALWAYS carries a trailing
    /// `0x80` marker (possibly followed by `0x00` filler) — never an
    /// already-block-aligned plaintext with no marker at all.
    pub fn decrypt_full(&self, enc_data: &[u8]) -> Result<Vec<u8>, SamError> {
        let iv = self.response_iv()?;
        let dec = aes128_cbc_decrypt_no_pad(&self.enc_key, &iv, enc_data)?;
        let mut i = dec.len();
        while i > 0 && dec[i - 1] == 0x00 {
            i -= 1;
        }
        if i > 0 && dec[i - 1] == 0x80 {
            Ok(dec[..i - 1].to_vec())
        } else {
            // No 0x80 marker found — per the doc comment above this should
            // not happen for a genuine EV2 Full response; fail closed rather
            // than silently trusting un-unpadded bytes as if they were the
            // plaintext.
            Err(SamError::Crypto(
                "CommMode.Full response: no ISO/IEC 9797-1 padding marker found in decrypted \
                 data — refusing to trust an un-padded response"
                    .to_string(),
            ))
        }
    }

    /// EV2 `CommMode.Full` response MAC, truncated to the odd-indexed 8
    /// bytes — the response-side mirror of [`Self::mac_command`]. Bead bi0
    /// rework 2026-07-15 (`punch::authenticate_and_read_token_id`) is this
    /// codebase's first response-MAC consumer. CONFIRMED on real hardware
    /// 2026-07-15 (zeit-terminal-01, bead 3l1, via an in-process
    /// `PUNCH_MAC_DIAG` brute-force against a real card): the field order
    /// below, using `self.cmd_ctr = N+1` (the card increments its counter
    /// BETWEEN command and response — see
    /// [`super::flows::read_token_id_software`]'s single `advance()` call),
    /// matches the card's actual response CMAC:
    ///
    /// `RC || CmdCtr[2, LE] || TI || EncData`
    ///
    /// where `RC` is the native response/status byte (`apdu::PICC_OK` for a
    /// successful `ReadData`) — the response has no command-header field to
    /// include (unlike the request side's `CmdHeader`), since only the
    /// status byte and returned data are on the wire.
    pub fn mac_response(&self, resp_code: u8, enc_data: &[u8]) -> [u8; 8] {
        let mut input = Vec::with_capacity(1 + 2 + 4 + enc_data.len());
        input.push(resp_code);
        input.extend_from_slice(&self.cmd_ctr.to_le_bytes());
        input.extend_from_slice(&self.ti);
        input.extend_from_slice(enc_data);
        truncate_mac_odd(&cmac_aes128(&self.mac_key, &input))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn hex16(s: &str) -> [u8; 16] {
        let v = hex::decode(s).unwrap();
        v.try_into().unwrap()
    }

    // RFC 4493 Section 4 test vectors. Key K = 2b7e1516 28aed2a6 abf71588
    // 09cf4f3c, plaintext is the first Mlen bytes of a fixed 64-byte message.
    const RFC4493_KEY: &str = "2b7e151628aed2a6abf7158809cf4f3c";

    // The RFC 4493 test messages are the first N bytes of this fixed 64-byte
    // string (each 16-byte line is one AES block).
    const RFC4493_M16: &str = "6bc1bee22e409f96e93d7e117393172a";
    const RFC4493_M32: &str = "ae2d8a571e03ac9c9eb76fac45af8e51";
    const RFC4493_M48: &str = "30c81c46a35ce411e5fbc1191a0a52ef";
    const RFC4493_M64: &str = "f69f2445df4f9b17ad2b417be66c3710";

    #[test]
    fn rfc4493_example1_empty_message() {
        let key = hex16(RFC4493_KEY);
        let tag = cmac_aes128(&key, b"");
        assert_eq!(hex::encode(tag), "bb1d6929e95937287fa37d129b756746");
    }

    #[test]
    fn rfc4493_example2_16_byte_message() {
        let key = hex16(RFC4493_KEY);
        let msg = hex::decode(RFC4493_M16).unwrap();
        assert_eq!(msg.len(), 16);
        let tag = cmac_aes128(&key, &msg);
        assert_eq!(hex::encode(tag), "070a16b46b4d4144f79bdd9dd04a287c");
    }

    #[test]
    fn rfc4493_example3_40_byte_message() {
        let key = hex16(RFC4493_KEY);
        let full = hex::decode(format!(
            "{RFC4493_M16}{RFC4493_M32}{RFC4493_M48}{RFC4493_M64}"
        ))
        .unwrap();
        assert_eq!(full.len(), 64);
        let msg = &full[..40];
        let tag = cmac_aes128(&key, msg);
        assert_eq!(hex::encode(tag), "dfa66747de9ae63030ca32611497c827");
    }

    #[test]
    fn rfc4493_example4_64_byte_message() {
        let key = hex16(RFC4493_KEY);
        let msg = hex::decode(format!(
            "{RFC4493_M16}{RFC4493_M32}{RFC4493_M48}{RFC4493_M64}"
        ))
        .unwrap();
        assert_eq!(msg.len(), 64);
        let tag = cmac_aes128(&key, &msg);
        assert_eq!(hex::encode(tag), "51f0bebf7e3b9d92fc49741779363cfe");
    }

    #[test]
    fn cbc_encrypt_rejects_unaligned_input() {
        let key = [0u8; 16];
        let iv = [0u8; 16];
        assert!(aes128_cbc_encrypt_no_pad(&key, &iv, &[0u8; 15]).is_err());
        assert!(aes128_cbc_encrypt_no_pad(&key, &iv, &[]).is_err());
    }

    #[test]
    fn cbc_encrypt_all_zero_block_matches_known_aes_ecb_vector() {
        // AES-128-ECB(key=0, block=0) is a widely-published sanity vector;
        // CBC with a zero IV over one zero block reduces to plain ECB.
        let key = [0u8; 16];
        let iv = [0u8; 16];
        let out = aes128_cbc_encrypt_no_pad(&key, &iv, &[0u8; 16]).unwrap();
        assert_eq!(hex::encode(out), "66e94bd4ef8a2c3b884cfa59ca342b2e");
    }

    #[test]
    fn diversify_key_rejects_out_of_range_input() {
        let master = [0u8; 16];
        assert!(diversify_key_aes128(&master, &[]).is_err());
        assert!(diversify_key_aes128(&master, &[0u8; 32]).is_err());
        assert!(diversify_key_aes128(&master, &[0u8; 31]).is_ok());
    }

    #[test]
    fn diversification_input_assembles_uid_keyno_aid() {
        let uid = [0x04, 0x11, 0x22, 0x33, 0x44, 0x55, 0x66];
        let aid = [0xAA, 0xBB, 0xCC];
        let m = diversification_input(&uid, 0x02, &aid);
        assert_eq!(
            m,
            [0x04, 0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x02, 0xAA, 0xBB, 0xCC]
        );
    }

    #[test]
    fn diversify_key_is_deterministic_and_key_dependent() {
        let master_a = [0x11u8; 16];
        let master_b = [0x22u8; 16];
        let input = [0xAAu8; 11];
        let a1 = diversify_key_aes128(&master_a, &input).unwrap();
        let a2 = diversify_key_aes128(&master_a, &input).unwrap();
        let b1 = diversify_key_aes128(&master_b, &input).unwrap();
        assert_eq!(a1, a2, "diversification must be deterministic");
        assert_ne!(a1, b1, "different master keys must diversify differently");
    }

    #[test]
    fn derive_host_session_keys_gives_distinct_ke_km() {
        let master = [0x42u8; 16];
        let rnd_a = [0x01u8; 16];
        let rnd_b = [0x02u8; 16];
        let keys = derive_host_session_keys(&master, &rnd_a, &rnd_b).unwrap();
        assert_ne!(keys.ke, keys.km);
        // Deterministic given the same inputs.
        let keys2 = derive_host_session_keys(&master, &rnd_a, &rnd_b).unwrap();
        assert_eq!(keys.ke, keys2.ke);
        assert_eq!(keys.km, keys2.km);
    }

    /// Deterministic KAT locking the reference-aligned Ke/Km derivation
    /// against regressions. NOT an externally-sourced test vector (none is
    /// publicly available for this construction) — self-consistent only:
    /// computed by hand-tracing `dumacp/smartcard`'s `AuthHostAV2` SV1/SV2
    /// construction against these fixed inputs, matching this module's
    /// implementation by construction. See HARDWARE-VERIFY on
    /// [`derive_host_session_keys`] — this test catches *regressions*, not
    /// correctness-against-real-hardware.
    #[test]
    fn derive_host_session_keys_matches_hand_traced_kat() {
        let master = hex16("000102030405060708090a0b0c0d0e0f");
        let rnd_a: [u8; 16] = (0..16u8).collect::<Vec<_>>().try_into().unwrap();
        let rnd_b: [u8; 16] = (16..32u8).collect::<Vec<_>>().try_into().unwrap();

        // SV2 = RndA[7..12] || RndB[7..12] || (RndB[0..5] ^ RndA[0..5]) || 0x82
        let mut sv2 = Vec::new();
        sv2.extend_from_slice(&rnd_a[7..12]);
        sv2.extend_from_slice(&rnd_b[7..12]);
        for i in 0..5 {
            sv2.push(rnd_b[i] ^ rnd_a[i]);
        }
        sv2.push(0x82);
        let expected_km = aes128_cbc_encrypt_no_pad(&master, &[0u8; 16], &sv2).unwrap();

        // SV1 = RndA[11..16] || RndB[11..16] || (RndB[4..9] ^ RndA[4..9]) || 0x81
        let mut sv1 = Vec::new();
        sv1.extend_from_slice(&rnd_a[11..16]);
        sv1.extend_from_slice(&rnd_b[11..16]);
        for i in 0..5 {
            sv1.push(rnd_b[4 + i] ^ rnd_a[4 + i]);
        }
        sv1.push(0x81);
        let expected_ke = aes128_cbc_encrypt_no_pad(&master, &[0u8; 16], &sv1).unwrap();

        let keys = derive_host_session_keys(&master, &rnd_a, &rnd_b).unwrap();
        assert_eq!(keys.km.to_vec(), expected_km);
        assert_eq!(keys.ke.to_vec(), expected_ke);
    }

    #[test]
    fn generate_offline_session_key_matches_reference_sv_construction() {
        // Independently recompute the reference's AES-128 SV construction and
        // single-block ECB, then compare against the function.
        let entry_key = hex16("00112233445566778899aabbccddeeff");
        let changecnt: u16 = 0x1234;

        let mut sv1a = [0x71u8; 16];
        sv1a[0] = 0x12;
        sv1a[1] = 0x34;
        let mut sv2a = [0x72u8; 16];
        sv2a[0] = 0x12;
        sv2a[1] = 0x34;
        let expected_ke = aes128_cbc_encrypt_no_pad(&entry_key, &[0u8; 16], &sv1a).unwrap();
        let expected_km = aes128_cbc_encrypt_no_pad(&entry_key, &[0u8; 16], &sv2a).unwrap();

        let (ke, km) = generate_offline_session_key(&entry_key, changecnt).unwrap();
        assert_eq!(ke.to_vec(), expected_ke);
        assert_eq!(km.to_vec(), expected_km);
        assert_ne!(ke, km, "SV1a(0x71) and SV2a(0x72) must diverge Ke from Km");
    }

    #[test]
    fn generate_offline_session_key_all_zero_entry_key_is_known_ecb_vector() {
        // Entry 1's factory-default current key is the all-zero AES-128 key.
        // With changecnt=0, SV1a = 00 00 71×14, so Ke = AES-ECB(0-key, SV1a);
        // pin both against a hand-computed AES-ECB block so a regression in
        // the byte order / fill bytes is caught without hardware.
        let (ke, km) = generate_offline_session_key(&[0u8; 16], 0).unwrap();

        let mut sv1a = [0x71u8; 16];
        sv1a[0] = 0x00;
        sv1a[1] = 0x00;
        let mut sv2a = [0x72u8; 16];
        sv2a[0] = 0x00;
        sv2a[1] = 0x00;
        let ke_ref = aes128_cbc_encrypt_no_pad(&[0u8; 16], &[0u8; 16], &sv1a).unwrap();
        let km_ref = aes128_cbc_encrypt_no_pad(&[0u8; 16], &[0u8; 16], &sv2a).unwrap();
        assert_eq!(ke.to_vec(), ke_ref);
        assert_eq!(km.to_vec(), km_ref);
    }

    #[test]
    fn generate_offline_session_key_is_changecnt_and_big_endian_dependent() {
        let entry_key = [0x5Au8; 16];
        let (ke0, _) = generate_offline_session_key(&entry_key, 0x0001).unwrap();
        let (ke1, _) = generate_offline_session_key(&entry_key, 0x0100).unwrap();
        // 0x0001 -> SV[0..2]=00 01; 0x0100 -> SV[0..2]=01 00. Big-endian, so
        // these MUST differ (a little-endian bug would collapse the two).
        assert_ne!(ke0, ke1, "changecnt must be encoded big-endian");
    }

    #[test]
    fn cbc_decrypt_is_inverse_of_cbc_encrypt() {
        let key = [0x77u8; 16];
        let iv = [0x00u8; 16];
        let data = [0xAAu8; 32];
        let enc = aes128_cbc_encrypt_no_pad(&key, &iv, &data).unwrap();
        let dec = aes128_cbc_decrypt_no_pad(&key, &iv, &enc).unwrap();
        assert_eq!(dec, data.to_vec());
    }

    #[test]
    fn cbc_decrypt_rejects_unaligned_input() {
        let key = [0u8; 16];
        let iv = [0u8; 16];
        assert!(aes128_cbc_decrypt_no_pad(&key, &iv, &[0u8; 15]).is_err());
        assert!(aes128_cbc_decrypt_no_pad(&key, &iv, &[]).is_err());
    }

    #[test]
    fn rotate_left_16_matches_reference_semantics() {
        let data: [u8; 16] = (0..16u8).collect::<Vec<_>>().try_into().unwrap();
        let rotated = rotate_left_16(&data, 2);
        // data[2..] || data[..2]
        let mut expected = data[2..].to_vec();
        expected.extend_from_slice(&data[..2]);
        assert_eq!(rotated.to_vec(), expected);

        // Rotating by 0 or 16 is a no-op (16 % 16 == 0).
        assert_eq!(rotate_left_16(&data, 0), data);
        assert_eq!(rotate_left_16(&data, 16), data);
    }

    #[test]
    fn host_auth_pass2_cmac_is_deterministic_and_extracts_odd_bytes() {
        let master = [0x55u8; 16];
        let rnd2 = [0x66u8; 12];
        let cmac2_a = host_auth_pass2_cmac(&master, &rnd2, 0x02);
        let cmac2_b = host_auth_pass2_cmac(&master, &rnd2, 0x02);
        assert_eq!(cmac2_a, cmac2_b, "must be deterministic");

        // Cross-check against manual odd-byte extraction of the full CMAC.
        let mut var1 = Vec::new();
        var1.extend_from_slice(&rnd2);
        var1.push(0x02);
        var1.extend_from_slice(&[0u8; 3]);
        let full = cmac_aes128(&master, &var1);
        let mut expected = [0u8; 8];
        for (i, b) in full.iter().enumerate() {
            if i % 2 != 0 {
                expected[i / 2] = *b;
            }
        }
        assert_eq!(cmac2_a, expected);

        // Different hostMode must change the tag.
        let cmac2_c = host_auth_pass2_cmac(&master, &rnd2, 0x01);
        assert_ne!(cmac2_a, cmac2_c);
    }

    #[test]
    fn derive_kex_is_deterministic_and_input_dependent() {
        let master = [0x99u8; 16];
        let rnd1 = [0x11u8; 12];
        let rnd2 = [0x22u8; 12];
        let kex1 = derive_kex(&master, &rnd1, &rnd2).unwrap();
        let kex2 = derive_kex(&master, &rnd1, &rnd2).unwrap();
        assert_eq!(kex1, kex2, "must be deterministic");

        let rnd2_other = [0x33u8; 12];
        let kex3 = derive_kex(&master, &rnd1, &rnd2_other).unwrap();
        assert_ne!(kex1, kex3, "different rnd2 must change Kex");
    }

    // -----------------------------------------------------------------
    // CMAC-with-IV: with IV=0 it must equal the plain (zero-IV) CMAC, so
    // the RFC 4493 known-answer vectors also pin the IV-capable variant.
    // -----------------------------------------------------------------

    #[test]
    fn cmac_with_zero_iv_matches_plain_cmac_on_rfc4493_vectors() {
        let key = hex16(RFC4493_KEY);
        let zero = [0u8; 16];

        assert_eq!(
            cmac_aes128_with_iv(&key, &zero, b""),
            cmac_aes128(&key, b"")
        );
        let m16 = hex::decode(RFC4493_M16).unwrap();
        assert_eq!(
            cmac_aes128_with_iv(&key, &zero, &m16),
            cmac_aes128(&key, &m16)
        );
        let full = hex::decode(format!(
            "{RFC4493_M16}{RFC4493_M32}{RFC4493_M48}{RFC4493_M64}"
        ))
        .unwrap();
        // 40-byte (incomplete final block) and 64-byte (complete) cases.
        assert_eq!(
            cmac_aes128_with_iv(&key, &zero, &full[..40]),
            cmac_aes128(&key, &full[..40])
        );
        assert_eq!(
            cmac_aes128_with_iv(&key, &zero, &full),
            cmac_aes128(&key, &full)
        );
    }

    #[test]
    fn cmac_with_iv_is_iv_dependent() {
        let key = hex16(RFC4493_KEY);
        let msg = hex::decode(RFC4493_M16).unwrap();
        let a = cmac_aes128_with_iv(&key, &[0u8; 16], &msg);
        let b = cmac_aes128_with_iv(&key, &[0x11u8; 16], &msg);
        assert_ne!(a, b, "a different chaining IV must change the CMAC");
    }

    #[test]
    fn truncate_mac_odd_extracts_bytes_1_3_5_through_15() {
        let full: [u8; 16] = (0..16u8).collect::<Vec<_>>().try_into().unwrap();
        assert_eq!(truncate_mac_odd(&full), [1, 3, 5, 7, 9, 11, 13, 15]);
    }

    // -----------------------------------------------------------------
    // SecureMessaging (liblo createfullProtectionCmd port).
    // -----------------------------------------------------------------

    #[test]
    fn secure_messaging_new_has_zero_seed_state() {
        let sm = SecureMessaging::new([0xAAu8; 16], [0xBBu8; 16]);
        assert_eq!(sm.cmd_ctr, 0);
        assert_eq!(sm.last_session_iv, [0u8; 16]);
        assert_eq!(sm.last_mac_iv, [0u8; 16]);
        assert_eq!(sm.cmd_ctr_be(), [0, 0, 0, 0]);
    }

    #[test]
    fn generate_enc_iv_structure_matches_reference() {
        // ctr=0 → myIV = 01010101 00000000 00000000 00000000; with the seed
        // last_session_iv = 0, the CBC reduces to a single ECB block, so
        // enc_iv = AES-ECB(ke, myIV).
        let ke = [0x5Au8; 16];
        let sm = SecureMessaging::new(ke, [0x00u8; 16]);
        let mut my_iv = vec![0x01u8; 4];
        my_iv.extend_from_slice(&[0x00u8; 12]); // three zero counters
        let expected = aes128_cbc_encrypt_no_pad(&ke, &[0u8; 16], &my_iv).unwrap();
        assert_eq!(sm.generate_enc_iv(true).unwrap().to_vec(), expected);
        // Decrypt IV uses fill byte 0x02 → must differ from the encrypt IV.
        assert_ne!(
            sm.generate_enc_iv(false).unwrap(),
            sm.generate_enc_iv(true).unwrap()
        );
    }

    #[test]
    fn create_full_protection_cmd_is_byte_exact_to_reference_scheme() {
        // Fixed inputs: ke, km, and a 61-byte "key entry" data field wrapped
        // in a plaintext ChangeKeyEntry command (80 C1 05 80 3D <61 bytes>).
        let ke = [0x11u8; 16];
        let km = [0x22u8; 16];
        let data61: Vec<u8> = (0..61u8).collect();
        let mut cmd = vec![0x80, 0xC1, 0x05, 0x80, 0x3D];
        cmd.extend_from_slice(&data61);

        let mut sm = SecureMessaging::new(ke, km);
        let wire = sm.create_full_protection_cmd(&cmd).unwrap();

        // --- Independently recompute every step, per the reference. ---
        // 1) enc IV (ctr=0, seed IV=0): AES-ECB(ke, 01×4 || 00×12).
        let mut my_iv = vec![0x01u8; 4];
        my_iv.extend_from_slice(&[0x00u8; 12]);
        let enc_iv: [u8; 16] = aes128_cbc_encrypt_no_pad(&ke, &[0u8; 16], &my_iv)
            .unwrap()
            .try_into()
            .unwrap();

        // 2) method-2 pad 61 -> 64 (0x80 then zeros), CBC-encrypt under ke.
        let mut padded = data61.clone();
        padded.push(0x80);
        while padded.len() % 16 != 0 {
            padded.push(0x00);
        }
        assert_eq!(padded.len(), 64);
        let enc_data = aes128_cbc_encrypt_no_pad(&ke, &enc_iv, &padded).unwrap();

        // 3) CMAC input: header + encData, Lc->72, counter(0) inserted at idx 2.
        let mut protected = vec![0x80, 0xC1, 0x05, 0x80, 72u8];
        protected.extend_from_slice(&enc_data);
        protected.splice(2..2, [0u8, 0, 0, 0]); // little-endian ctr=0
        assert_eq!(protected.len(), 73);

        // 4) split MAC: CBC first 64 bytes under km (IV=0), tail(9) CMAC'd
        //    with the chained IV, odd-byte truncated to 8.
        let cbc = aes128_cbc_encrypt_no_pad(&km, &[0u8; 16], &protected[..64]).unwrap();
        let chained_iv: [u8; 16] = cbc[cbc.len() - 16..].try_into().unwrap();
        let full_mac = cmac_aes128_with_iv(&km, &chained_iv, &protected[64..]);
        let mac8 = truncate_mac_odd(&full_mac);

        // 5) wire = header + encData(Lc=72) + mac8 — NO trailing Le (liblo's
        //    finalFullProtectedCmd ends at the MAC; a trailing Le makes the SAM
        //    reject ChangeKeyEntry with 6A84).
        let mut expected = vec![0x80, 0xC1, 0x05, 0x80, 72u8];
        expected.extend_from_slice(&enc_data);
        expected.extend_from_slice(&mac8);

        assert_eq!(wire, expected, "wire APDU must match the reference scheme");
        assert_eq!(wire.len(), 5 + 64 + 8); // 77 bytes, no Le
        assert_eq!(wire[4], 72, "Lc = encData(64) + MAC(8)");

        // Session state advanced: enc IV recorded, MAC IV = prefix CBC block.
        assert_eq!(sm.last_session_iv, enc_iv);
        assert_eq!(sm.last_mac_iv, chained_iv);
        assert_eq!(sm.cmd_ctr, 0, "counter is bumped by transmit, not the wrap");
    }

    #[test]
    fn create_full_protection_cmd_accepts_lc_data_le_shape_and_carries_le_after_mac() {
        // The case-4 "LC DATA LE" shape (SAM_DumpSecretKey, bead eyc) is now
        // supported. The trailing Le must survive — unencrypted — onto the
        // wire AFTER the 8-byte MAC, and the wire must be exactly one byte
        // (the Le) longer than the same command wrapped without an Le.
        let ke = [0x33u8; 16];
        let km = [0x44u8; 16];
        // 80 D6 02 00 06 <keyno keyver + 4 div bytes> — 6-byte data field.
        let base = vec![
            0x80, 0xD6, 0x02, 0x00, 0x06, 0x02, 0x01, 0xDE, 0xAD, 0xBE, 0xEF,
        ];

        let mut sm_no_le = SecureMessaging::new(ke, km);
        let wire_no_le = sm_no_le.create_full_protection_cmd(&base).unwrap();

        let mut with_le = base.clone();
        with_le.push(0x00); // trailing Le
        let mut sm_le = SecureMessaging::new(ke, km);
        let wire_le = sm_le.create_full_protection_cmd(&with_le).unwrap();

        // Le adds exactly one trailing byte over the no-Le wrap.
        assert_eq!(wire_le.len(), wire_no_le.len() + 1);
        assert_eq!(*wire_le.last().unwrap(), 0x00, "Le rides after the MAC");
        // The header+encData prefix (everything before the 8-byte MAC) is
        // identical between the two wraps.
        let prefix_len = wire_no_le.len() - 8;
        assert_eq!(&wire_le[..prefix_len], &wire_no_le[..prefix_len]);
        // But the Le is part of the CMAC input (protectedCmd), so the MAC
        // itself differs — proving the Le is authenticated, not just appended.
        assert_ne!(
            &wire_le[prefix_len..prefix_len + 8],
            &wire_no_le[prefix_len..]
        );
    }

    #[test]
    fn create_full_protection_cmd_rejects_unsupported_apdu_shapes() {
        // A bare "CLA INS P1 P2 LE" (case-2, no LC/data) is still unsupported.
        let mut sm = SecureMessaging::new([0u8; 16], [0u8; 16]);
        assert!(sm
            .create_full_protection_cmd(&[0x80, 0xC1, 0x00, 0x00, 0x00])
            .is_err());
    }

    // -----------------------------------------------------------------
    // verify_and_decrypt_response / full_protection_round_trip
    // (SAM_DumpSecretKey, bead 4ty rework 2026-07-15).
    // -----------------------------------------------------------------

    /// Build a Full-protection SAM response the way the real AV3 SAM does —
    /// the inverse of `verify_and_decrypt_response` as called by
    /// `full_protection_round_trip`: the response is processed at `CmdCtr = 1`
    /// (request 0, +1), IV chains zero, counter BIG-ENDIAN, and
    /// `MAC8 = truncate_odd(CMAC(Km, IV=0, SW || CmdCtr_BE || encData))` over
    /// the ciphertext. Pinned to real hardware by
    /// `response_mac_matches_captured_av3_hardware_vector`.
    fn mock_sam_full_protection_response(
        ke: &[u8; 16],
        km: &[u8; 16],
        plain_data: &[u8],
        sw: [u8; 2],
    ) -> Vec<u8> {
        let ctr_be = 1u32.to_be_bytes(); // response CmdCtr = request(0) + 1

        let enc_data = if plain_data.is_empty() {
            Vec::new()
        } else {
            let mut padded = plain_data.to_vec();
            padded.push(0x80);
            while padded.len() % 16 != 0 {
                padded.push(0x00);
            }
            // Decrypt IV = AES-CBC(Ke, 0, [0x02;4] || CmdCtr_BE × 3) — the
            // `generate_enc_iv(false)` value at CmdCtr=1, session IV reset.
            let mut my_iv = vec![0x02u8; 4];
            for _ in 1..4 {
                my_iv.extend_from_slice(&ctr_be);
            }
            let iv: [u8; 16] = aes128_cbc_encrypt_no_pad(ke, &[0u8; 16], &my_iv)
                .unwrap()
                .try_into()
                .unwrap();
            aes128_cbc_encrypt_no_pad(ke, &iv, &padded).unwrap()
        };

        let mut mac_input = Vec::new();
        mac_input.extend_from_slice(&sw);
        mac_input.extend_from_slice(&ctr_be);
        mac_input.extend_from_slice(&enc_data);
        let mac8 = truncate_mac_odd(&cmac_aes128_with_iv(km, &[0u8; 16], &mac_input));

        let mut wire = enc_data;
        wire.extend_from_slice(&mac8);
        wire.extend_from_slice(&sw);
        wire
    }

    #[test]
    fn full_protection_round_trip_recovers_plaintext_and_advances_state() {
        use crate::reader::sam::mock::MockTransport;

        let ke = [0x01u8; 16];
        let km = [0x02u8; 16];
        let plain_data = [0xAAu8; 16]; // e.g. a "diversified key" payload
        let sw = [0x90, 0x00];

        // Response is verified at CmdCtr=1 (request 0, +1) with zero IVs —
        // see `full_protection_round_trip`.
        let plain_cmd = vec![0x80, 0xD6, 0x02, 0x00, 0x05, 0x01, 0x00, 0xAA, 0xAA, 0xAA];
        let response = mock_sam_full_protection_response(&ke, &km, &plain_data, sw);

        let mut sam = MockTransport::new();
        sam.push_ok(response);

        let mut sm = SecureMessaging::new(ke, km);
        let data = sm.full_protection_round_trip(&mut sam, &plain_cmd).unwrap();

        let mut expected = plain_data.to_vec();
        expected.extend_from_slice(&sw);
        assert_eq!(data, expected);

        // Counter advanced to 1 by the one-shot exchange.
        assert_eq!(sm.cmd_ctr, 1);
    }

    #[test]
    fn full_protection_round_trip_fails_closed_on_mac_mismatch() {
        use crate::reader::sam::mock::MockTransport;

        let ke = [0x01u8; 16];
        let km = [0x02u8; 16];
        // Built with the WRONG km — the MAC will not verify.
        let plain_cmd = vec![0x80, 0xD6, 0x02, 0x00, 0x05, 0x01, 0x00, 0xAA, 0xAA, 0xAA];
        let response =
            mock_sam_full_protection_response(&ke, &[0xFFu8; 16], &[0xAAu8; 16], [0x90, 0x00]);

        let mut sam = MockTransport::new();
        sam.push_ok(response);

        let mut sm = SecureMessaging::new(ke, km);
        let err = sm
            .full_protection_round_trip(&mut sam, &plain_cmd)
            .unwrap_err();
        assert!(matches!(err, SamError::CmacMismatch(_)));
    }

    /// Regression: pin the AV3 response-MAC construction to the exact vector
    /// captured from real MF4SAM3 hardware (bead eyc, 2026-07-15), so the
    /// scheme can never silently change. Construction:
    ///   MAC8 = truncate_odd( CMAC(Km, IV=0, SW[2] || CmdCtr_BE[4] || encData) )
    /// with CmdCtr = 1 (request 0, response +1), big-endian. Km/encData/SW are
    /// the logged session values; TARGET is the SAM's actual response MAC8.
    #[test]
    fn response_mac_matches_captured_av3_hardware_vector() {
        let km: [u8; 16] = hex::decode("1375956fc2caaa831e914a25a8c1a399")
            .unwrap()
            .try_into()
            .unwrap();
        let enc = hex::decode("d6331ac74f9bec9077b15ca567d9e68432460fcf9965be4bb87186afc98e6a0e")
            .unwrap();
        let target = hex::decode("2f025dd92816754b").unwrap();

        let mut mac_input = Vec::new();
        mac_input.extend_from_slice(&[0x90, 0x00]); // SW
        mac_input.extend_from_slice(&1u32.to_be_bytes()); // CmdCtr = 1, big-endian
        mac_input.extend_from_slice(&enc);

        let mac8 = truncate_mac_odd(&cmac_aes128_with_iv(&km, &[0u8; 16], &mac_input));
        assert_eq!(mac8.to_vec(), target, "AV3 response-MAC construction drift");
    }

    // -----------------------------------------------------------------
    // DESFire EV2 software crypto (crc32_desfire, session-key derivation,
    // DesfireEv2Session) — bead 4ty rework 2026-07-15.
    // -----------------------------------------------------------------

    #[test]
    fn crc32_desfire_matches_the_well_known_public_test_vector() {
        // The canonical CRC-32/ISO-HDLC check value for the ASCII string
        // "123456789" — published everywhere this CRC variant is described.
        assert_eq!(crc32_desfire(b"123456789"), 0x340B_C6D9);
    }

    #[test]
    fn build_change_key_self_cryptogram_is_newkey_keyver_with_no_crc() {
        let new_key = [0x5Au8; 16];
        let key_ver = 0x01u8;
        let cryptogram = build_change_key_self_cryptogram(&new_key, key_ver);

        // EV2 self-change: exactly NewKey[16] || KeyVer[1], NO CRC, NO padding
        // (encrypt_full applies the 0x80 pad). See the fn doc + DesfireEv3 ref.
        assert_eq!(cryptogram.len(), 17);
        assert_eq!(&cryptogram[0..16], &new_key);
        assert_eq!(cryptogram[16], key_ver);
    }

    #[test]
    fn change_key_self_cryptogram_encrypts_to_32_bytes_with_0x80_pad() {
        // encrypt_full must ISO-pad the 17-byte cryptogram (0x80 || 00…) to a
        // single 32-byte AES block — matching the EV2/EV3 reference's
        // `keyNew || KEY_VERSION || 0x80-pad` self-change data.
        let session = DesfireEv2Session::new([0x11u8; 16], [0x22u8; 16], [1, 2, 3, 4]);
        let cryptogram = build_change_key_self_cryptogram(&[0x5Au8; 16], 0x01);
        let enc = session.encrypt_full(&cryptogram).unwrap();
        assert_eq!(enc.len(), 32, "17 bytes + 0x80 pad → one 32-byte block set");
    }

    #[test]
    fn ev2_mac_command_puts_command_byte_first() {
        // Pin the EV2 command-MAC input order: Cmd || CmdCtr[2 LE] || TI ||
        // Header || EncData (liblo generateMAC). Recompute independently.
        let mac_key = [0x02u8; 16];
        let session = DesfireEv2Session::new([0x01u8; 16], mac_key, [0xAA, 0xBB, 0xCC, 0xDD]);
        let header = [0xC4u8, 0x03]; // Cmd=C4, CmdHeader=KeyNo 0x03
        let enc = [0x77u8; 32];

        let mut expected_input = Vec::new();
        expected_input.push(0xC4); // Cmd first
        expected_input.extend_from_slice(&0u16.to_le_bytes()); // CmdCtr
        expected_input.extend_from_slice(&[0xAA, 0xBB, 0xCC, 0xDD]); // TI
        expected_input.push(0x03); // CmdHeader (KeyNo)
        expected_input.extend_from_slice(&enc);
        let expected = truncate_mac_odd(&cmac_aes128(&mac_key, &expected_input));

        assert_eq!(session.mac_command(&header, &enc), expected);
    }

    #[test]
    fn derive_desfire_ev2_session_keys_gives_distinct_deterministic_keys() {
        let key = [0x11u8; 16];
        let rnd_a = [0x22u8; 16];
        let rnd_b = [0x33u8; 16];
        let (enc1, mac1) = derive_desfire_ev2_session_keys(&key, &rnd_a, &rnd_b);
        let (enc2, mac2) = derive_desfire_ev2_session_keys(&key, &rnd_a, &rnd_b);
        assert_eq!(enc1, enc2, "must be deterministic");
        assert_eq!(mac1, mac2);
        assert_ne!(enc1, mac1, "SV1/SV2 prefixes must diverge enc from mac key");

        let rnd_a_other = [0x44u8; 16];
        let (enc3, _) = derive_desfire_ev2_session_keys(&key, &rnd_a_other, &rnd_b);
        assert_ne!(enc1, enc3, "different RndA must change the derived key");
    }

    #[test]
    fn desfire_ev2_session_command_and_response_iv_diverge_and_advance() {
        let session = DesfireEv2Session::new([0x01u8; 16], [0x02u8; 16], [0xAA, 0xBB, 0xCC, 0xDD]);
        let iv_cmd = session.command_iv().unwrap();
        let iv_resp = session.response_iv().unwrap();
        assert_ne!(iv_cmd, iv_resp, "command/response IV prefixes must diverge");

        let mut session2 =
            DesfireEv2Session::new([0x01u8; 16], [0x02u8; 16], [0xAA, 0xBB, 0xCC, 0xDD]);
        assert_eq!(session2.cmd_ctr, 0);
        session2.advance();
        assert_eq!(session2.cmd_ctr, 1);
        assert_ne!(
            session2.command_iv().unwrap(),
            iv_cmd,
            "IV must change once the command counter advances"
        );
    }

    #[test]
    fn desfire_ev2_session_encrypt_full_always_pads_even_when_aligned() {
        let session = DesfireEv2Session::new([0x01u8; 16], [0x02u8; 16], [0u8; 4]);
        // Exactly 16 bytes (already aligned) — encrypt_full must still pad
        // to 32 bytes (see the HARDWARE-VERIFY doc comment on the method).
        let enc = session.encrypt_full(&[0xABu8; 16]).unwrap();
        assert_eq!(enc.len(), 32);
    }

    #[test]
    fn desfire_ev2_session_mac_command_is_deterministic_and_input_dependent() {
        let session = DesfireEv2Session::new([0x01u8; 16], [0x02u8; 16], [1, 2, 3, 4]);
        let header = [0xC4u8, 0x00];
        let enc_data = [0xAAu8; 32];
        let mac1 = session.mac_command(&header, &enc_data);
        let mac2 = session.mac_command(&header, &enc_data);
        assert_eq!(mac1, mac2, "must be deterministic");

        let other_header = [0xC4u8, 0x01];
        let mac3 = session.mac_command(&other_header, &enc_data);
        assert_ne!(mac1, mac3, "a different header must change the MAC");
    }

    /// Encrypt `plain` the way the CARD would for a `CommMode.Full`
    /// response — same ISO/IEC 9797-1 method-2 pad as [`DesfireEv2Session::encrypt_full`],
    /// but under [`DesfireEv2Session::response_iv`] (the PICC -> PCD IV,
    /// which deliberately differs from the PCD -> PICC `command_iv`
    /// [`DesfireEv2Session::encrypt_full`] uses) — the test-side mirror of
    /// [`DesfireEv2Session::decrypt_full`], since this project has no
    /// separate "card-side" crypto implementation to generate real response
    /// fixtures from.
    fn encrypt_as_response(session: &DesfireEv2Session, plain: &[u8]) -> Vec<u8> {
        let mut padded = plain.to_vec();
        padded.push(0x80);
        while !padded.len().is_multiple_of(16) {
            padded.push(0x00);
        }
        let iv = session.response_iv().unwrap();
        aes128_cbc_encrypt_no_pad(&session.enc_key, &iv, &padded).unwrap()
    }

    #[test]
    fn desfire_ev2_session_decrypt_full_round_trips_through_response_encryption() {
        let session = DesfireEv2Session::new([0x33u8; 16], [0x44u8; 16], [5, 6, 7, 8]);
        let token_id = [0xABu8; 16];
        let enc = encrypt_as_response(&session, &token_id);
        let dec = session.decrypt_full(&enc).unwrap();
        assert_eq!(dec, token_id.to_vec());
    }

    #[test]
    fn desfire_ev2_session_decrypt_full_recovers_shorter_than_block_plaintext() {
        // A token-id shorter than one block still round-trips through the
        // unconditional 0x80/00-pad scheme (not this project's actual token-id
        // length, but exercises the unpad boundary condition).
        let session = DesfireEv2Session::new([0x55u8; 16], [0x66u8; 16], [1, 1, 1, 1]);
        let short = [0x11u8; 4];
        let enc = encrypt_as_response(&session, &short);
        assert_eq!(enc.len(), 16, "4 bytes + 0x80 pad fits one 16-byte block");
        let dec = session.decrypt_full(&enc).unwrap();
        assert_eq!(dec, short.to_vec());
    }

    #[test]
    fn desfire_ev2_session_decrypt_full_fails_closed_on_missing_pad_marker() {
        // A ciphertext that decrypts to all-zero bytes (no 0x80 marker
        // anywhere) must be rejected rather than silently treated as valid
        // unpadded plaintext.
        let session = DesfireEv2Session::new([0x77u8; 16], [0x88u8; 16], [2, 2, 2, 2]);
        let iv = session.response_iv().unwrap();
        let enc = aes128_cbc_encrypt_no_pad(&session.enc_key, &iv, &[0u8; 16]).unwrap();
        let err = session.decrypt_full(&enc).unwrap_err();
        assert!(matches!(err, SamError::Crypto(_)));
    }

    #[test]
    fn desfire_ev2_session_mac_response_is_deterministic_input_dependent_and_distinct_from_mac_command(
    ) {
        let session = DesfireEv2Session::new([0x01u8; 16], [0x02u8; 16], [1, 2, 3, 4]);
        let enc_data = [0xAAu8; 16];
        let mac1 = session.mac_response(0x00, &enc_data);
        let mac2 = session.mac_response(0x00, &enc_data);
        assert_eq!(mac1, mac2, "must be deterministic");

        let mac_other_rc = session.mac_response(0x7E, &enc_data);
        assert_ne!(
            mac1, mac_other_rc,
            "a different response code must change the MAC"
        );

        let mac_other_data = session.mac_response(0x00, &[0xBBu8; 16]);
        assert_ne!(
            mac1, mac_other_data,
            "different enc_data must change the MAC"
        );

        // A different CmdCtr (as `mac_command` also keys on) must change the
        // response MAC too — pins that `cmd_ctr` actually feeds the input,
        // not just `resp_code`/`enc_data`.
        let mut advanced = DesfireEv2Session::new([0x01u8; 16], [0x02u8; 16], [1, 2, 3, 4]);
        advanced.advance();
        let mac_advanced = advanced.mac_response(0x00, &enc_data);
        assert_ne!(mac1, mac_advanced, "a different CmdCtr must change the MAC");
    }
}
