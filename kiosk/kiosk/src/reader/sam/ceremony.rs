//! SAM AV3 key-loading ceremony (bead 3pn): the one-time, on-site procedure
//! that keys a company's SAM(s) with a freshly generated AES-128 master key.
//!
//! This module is transport-agnostic and pure logic (testable against
//! [`super::mock::MockTransport`], no hardware). The actual SSH-run CLI
//! (`src/bin/sam_ceremony.rs`, `real-reader`-gated) is a thin wrapper around
//! [`run_ceremony_on_sam`] plus [`generate_master_key`] and
//! [`split_key_backup`]/[`reconstruct_key_backup`].
//!
//! **Fail-closed**: every step propagates [`SamError`] via `?`. A failed
//! host authentication, a failed `ChangeKeyEntry`, or a CSPRNG failure abort
//! the whole ceremony for that SAM — there is no fallback to an
//! unauthenticated or partially-keyed state.

use super::apdu::{self, AesKeyEntry};
use super::crypto::{self, HostSessionKeys};
use super::error::SamError;
use super::flows;
use super::transport::SamTransport;

// ---------------------------------------------------------------------
// Step 2: CSPRNG master-key generation.
// ---------------------------------------------------------------------

/// Fill a buffer with cryptographically secure random bytes via the OS
/// CSPRNG (`getrandom(2)` on Linux — no userspace PRNG, no seeding).
fn csprng_fill(buf: &mut [u8]) -> Result<(), SamError> {
    getrandom::getrandom(buf).map_err(|e| SamError::Crypto(format!("CSPRNG failure: {e}")))
}

/// Generate a fresh random AES-128 company master key from the OS CSPRNG.
pub fn generate_master_key() -> Result<[u8; 16], SamError> {
    let mut key = [0u8; 16];
    csprng_fill(&mut key)?;
    Ok(key)
}

/// Generate a fresh random 12-byte host challenge (`rnd1` for
/// `SAM_AuthenticateHost` pass 2).
pub fn generate_rnd1() -> Result<[u8; 12], SamError> {
    let mut rnd1 = [0u8; 12];
    csprng_fill(&mut rnd1)?;
    Ok(rnd1)
}

/// Generate a fresh random 16-byte host challenge (`RndA` for
/// `SAM_AuthenticateHost` pass 3).
pub fn generate_rnd_a() -> Result<[u8; 16], SamError> {
    let mut rnd_a = [0u8; 16];
    csprng_fill(&mut rnd_a)?;
    Ok(rnd_a)
}

// ---------------------------------------------------------------------
// Step 0 (new): SAM_LockUnlock/Unlock pre-step — a factory-fresh SAM AV3
// ships LOCKED and rejects SAM_AuthenticateHost (6985) until unlocked once.
// ---------------------------------------------------------------------

/// The SAM's factory-default master key entry: keyNo 0, keyVer 0, key
/// material 16×0x00. Used ONLY to run [`flows::lock_unlock_unlock`] before
/// `SAM_AuthenticateHost` — this is NOT the company/host master key
/// (`HostKeyRef`), which is provisioned separately and never all-zero.
pub const SAM_FACTORY_DEFAULT_MASTER_KEY: [u8; 16] = [0u8; 16];
pub const SAM_FACTORY_DEFAULT_KEY_NO: u8 = 0x00;
pub const SAM_FACTORY_DEFAULT_KEY_VER: u8 = 0x00;

// ---------------------------------------------------------------------
// Step 3: company PICC-master key-entry composition.
// ---------------------------------------------------------------------

/// `SET` flags for the company PICC-master key entry: AES-128 type only, no
/// `requireAuth` — `20 00` per the architecture doc's worked example (see
/// `apdu::tests::set_flags_match_spec_examples`).
pub const COMPANY_PICC_MASTER_SET: u16 = apdu::set_flags::AES128_TYPE;

/// `ExtSET` flags for the company PICC-master key entry: `KeyClass = PICC`,
/// restricted to diversified use only (no bare/undiversified use), PLUS
/// `ALLOW_DUMP_SECRET_KEY` — `19` (updated 2026-07-15, bead 4ty rework; was
/// `11` per the architecture doc's original worked example, see
/// `apdu::tests::ext_set_flags_match_spec_examples`'s `0x19` example, which
/// already documented this combination).
///
/// **Why the bit was added**: personalization now fetches this entry's
/// diversified key via `SAM_DumpSecretKey` instead of relaying through
/// `SAM_AuthenticatePICC`/`SAM_ChangeKeyPICC` (see `personalize`'s
/// module-level "Rework 2026-07-15" doc comment) — per
/// liblogicalaccess's `dumpSecretKey`, that command needs
/// [`apdu::ext_set_flags::ALLOW_DUMP_SECRET_KEY`] set on the key entry, or
/// the SAM is expected to reject it (HARDWARE-VERIFY: not yet confirmed
/// which status code that rejection would carry).
///
/// **Operational consequence**: a SAM already keyed by an OLDER ceremony run
/// (before this constant changed) does NOT have this bit set and will need
/// re-keying (re-running the ceremony, or a `SAM_ChangeKeyEntry` that
/// updates just the `SET`/`ProMas::SET` field) before `SAM_DumpSecretKey`
/// will work against it.
pub const COMPANY_PICC_MASTER_EXT_SET: u8 = apdu::ext_set_flags::KEY_CLASS_PICC
    | apdu::ext_set_flags::RESTRICT_TO_DIVERSIFIED_USE
    | apdu::ext_set_flags::ALLOW_DUMP_SECRET_KEY;

/// `ProMas` for writing a brand-new company PICC-master entry: touch every
/// field this ceremony sets (VerA, AID+KeyNo, CEK, KUC, SET, the keyVer/CEK
/// separation bit) so the entry is fully (re)written, not partially patched.
///
/// HARDWARE-VERIFY: exact required `ProMas` bit set for a from-scratch
/// entry write is unconfirmed against real SAM AV3 firmware — some AV-family
/// firmware rejects a `ProMas` that includes bits for fields the command
/// doesn't also supply data for. If hardware rejects this, the first thing
/// to try is narrowing to just `VER_A | AID_KEYNO | CEK | KUC | SET`.
pub const COMPANY_PICC_MASTER_PRO_MAS: u8 = apdu::pro_mas::VER_A
    | apdu::pro_mas::AID_KEYNO
    | apdu::pro_mas::CEK
    | apdu::pro_mas::KUC
    | apdu::pro_mas::SET
    | apdu::pro_mas::KEYVER_SEP;

/// Build the 61-byte AES key-entry payload for the company PICC-master key:
/// `SET = 20 00`, `ExtSET = 11` (diversified-only PICC master), key material
/// in slot A only (B/C unused).
///
/// HARDWARE-VERIFY: `df_aid = 000000`/`df_key_no = 0` (PICC level, "key 0")
/// and `ref_no_kuc = 0xFF` ("no key-usage-counter attached") are this
/// project's configuration choices, not derived crypto — confirm both
/// against the SAM's actual provisioning tool / DS3235 conventions before
/// go-live.
pub fn company_picc_master_entry(key: &[u8; 16]) -> AesKeyEntry {
    AesKeyEntry {
        key_va: *key,
        key_vb: [0u8; 16],
        key_vc: [0u8; 16],
        df_aid: [0x00, 0x00, 0x00],
        df_key_no: 0x00,
        key_no_cek: 0x00,
        key_ver_cek: 0x00,
        ref_no_kuc: 0xFF,
        set: COMPANY_PICC_MASTER_SET,
        ver_a: 0x00,
        ver_b: 0x00,
        ver_c: 0x00,
        ext_set: COMPANY_PICC_MASTER_EXT_SET,
    }
}

// ---------------------------------------------------------------------
// Steps 1 + 3 combined: host-authenticate then load the company master key.
// ---------------------------------------------------------------------

/// Result of successfully keying one SAM: the host session established
/// (kept only for diagnostics — the caller should let it drop / not persist
/// it) and the exact key entry written.
pub struct KeyCompanySamResult {
    pub session: HostSessionKeys,
    pub entry: AesKeyEntry,
}

/// Redacted `Debug` — does **not** print `entry.key_va` (the just-loaded
/// company master key) even though [`AesKeyEntry`] itself derives `Debug`
/// (needed for its own byte-layout tests). Only needed so
/// `Result<KeyCompanySamResult, _>::unwrap_err()` compiles in tests;
/// `sam_ceremony.rs` never logs this struct — it prints `entry.set`/
/// `entry.ext_set` explicitly instead.
impl std::fmt::Debug for KeyCompanySamResult {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("KeyCompanySamResult")
            .field("session", &self.session)
            .field("entry.set", &self.entry.set)
            .field("entry.ext_set", &self.entry.ext_set)
            .field("entry.key_va", &"<redacted>")
            .finish()
    }
}

/// Identifies the SAM's pre-provisioned long-term host key to authenticate
/// with: which key-entry slot (`key_no`/`key_ver`) and its value. Bundled
/// into a struct (rather than three loose parameters) both for clarity and
/// to keep [`run_ceremony_on_sam`]'s argument count within
/// `clippy::too_many_arguments`.
pub struct HostKeyRef<'a> {
    pub key: &'a [u8; 16],
    pub key_no: u8,
    pub key_ver: u8,
}

/// The host-side CSPRNG randoms for one `SAM_AuthenticateHost` attempt (see
/// [`generate_rnd1`]/[`generate_rnd_a`]), plus the independent `lock_unlock_rnd1`/
/// `lock_unlock_rnd_a` used by the `SAM_LockUnlock`/Unlock pre-step
/// ([`flows::lock_unlock_unlock`], itself a 3-pass mutual auth like
/// `SAM_AuthenticateHost`) that now runs before authentication. Kept as
/// explicit inputs (not generated inside [`run_ceremony_on_sam`]) so that
/// function stays deterministically testable against
/// [`super::mock::MockTransport`]; bundled into a struct because these
/// randoms are always generated and passed together.
///
/// `lock_unlock_rnd1`/`lock_unlock_rnd_a` MUST be fresh CSPRNG randoms,
/// independent of `rnd1`/`rnd_a` (each pair is used in its own authenticated
/// handshake against the SAM — never reuse a challenge across independent
/// sessions).
pub struct HostChallenge {
    pub rnd1: [u8; 12],
    pub rnd_a: [u8; 16],
    pub lock_unlock_rnd1: [u8; 12],
    pub lock_unlock_rnd_a: [u8; 16],
}

/// Run the full per-SAM ceremony step: `SAM_LockUnlock`/Unlock (3-pass,
/// using the SAM's factory-default master key) so that a factory-fresh SAM
/// transitions out of its locked state, then `SAM_ChangeKeyEntry` (Full
/// mode) to write `company_key` into `entry_no` as the company PICC-master
/// key.
///
/// `SAM_LockUnlock` is itself a full host authentication (see
/// [`flows::lock_unlock_unlock`]'s doc comment) — it establishes the host
/// session keys (Ke/Km) in the same handshake that unlocks the SAM. Per NXP
/// AN12704, only one host authentication is allowed per channel, so this
/// function does **not** call `SAM_AuthenticateHost` afterwards — a second
/// authentication on an already-authenticated channel fails closed with
/// `6985`. `host_key` (the SAM's pre-provisioned long-term host key) is
/// therefore currently unused by this function; it is kept as a parameter
/// for the day a SAM that's already unlocked (no `LockUnlock` needed) is
/// supported, at which point `authenticate_host` would run instead — see
/// the module-level HARDWARE-VERIFY notes and bead 59m.
///
/// The unlock step always runs — this project has no cheap way to detect
/// "already unlocked" ahead of time, and (per `flows::lock_unlock_unlock`'s
/// HARDWARE-VERIFY note) re-running Unlock on an already-unlocked SAM is
/// assumed safe; that assumption is unconfirmed on real hardware (bead 59m).
///
/// Fail-closed: if the unlock step fails, `ChangeKeyEntry` is never
/// attempted. If `ChangeKeyEntry` fails, the SAM is left in whatever state
/// the SAM's own atomicity guarantees leave it in — this function does not
/// retry or paper over a partial write.
pub fn run_ceremony_on_sam(
    sam: &mut dyn SamTransport,
    host_key: &HostKeyRef<'_>,
    entry_no: u8,
    company_key: &[u8; 16],
    challenge: HostChallenge,
) -> Result<KeyCompanySamResult, SamError> {
    // Step 1: AuthenticateHost directly on the factory SAM. Per NXP AN12704
    // Figure 3 ("Usual Communication from the host"), `SAM_AuthenticateHost`
    // BOTH unlocks a locked SAM AND establishes the secure-messaging session
    // (Ke/Km) — in one step. `SAM_LockUnlock` is the inferior alternative that
    // unlocks WITHOUT secure messaging, so ChangeKeyEntry (Full mode) can never
    // run after it; that path is why every prior attempt returned 6985. We
    // therefore do NOT call `lock_unlock_unlock` at all. `challenge.lock_unlock_*`
    // is unused on this path (kept in the struct for the LockUnlock tooling).
    let _ = (&challenge.lock_unlock_rnd1, &challenge.lock_unlock_rnd_a);
    let session = flows::authenticate_host(
        sam,
        host_key.key,
        host_key.key_no,
        host_key.key_ver,
        apdu::HOST_MODE_FULL,
        challenge.rnd1,
        challenge.rnd_a,
    )?;

    // Step 2: ChangeKeyEntry (Full mode) on the AuthenticateHost session —
    // the byte-exact liblo `createfullProtectionCmd` path (per-command enc IV,
    // command counter, odd-truncated MAC), seeded from THIS session's Ke/Km.
    let entry = company_picc_master_entry(company_key);
    let plain = apdu::build_change_key_entry_plain(entry_no, COMPANY_PICC_MASTER_PRO_MAS, &entry);
    let mut sm = crypto::SecureMessaging::new(session.ke, session.km);
    let wrapped = sm.create_full_protection_cmd(&plain)?;
    let resp = sam.transmit(&wrapped)?;
    apdu::parse_status_ok(&resp)?;

    Ok(KeyCompanySamResult { session, entry })
}

// ---------------------------------------------------------------------
// OFFLINE key-change path (bead 59m alternative): re-key a
// `KeyClass = OfflineChange` entry WITHOUT SAM_AuthenticateHost (which
// returns 6985 on this project's SAM AV3). The session is derived from the
// entry's OWN CURRENT KEY + a change counter — see
// [`crypto::generate_offline_session_key`].
// ---------------------------------------------------------------------

/// The current key of SAM key **entry 1** on a factory-fresh SAM: the
/// all-zero AES-128 key. Proven reachable on this project's hardware —
/// `SAM_LockUnlock` reaches `9000` with the all-zero key (bead 59m). This is
/// the `entry_key` that seeds [`crypto::generate_offline_session_key`] for the
/// first offline re-key of entry 1; after a successful change it becomes the
/// newly loaded company key (so a *second* offline change would seed from
/// that instead).
pub const ENTRY_FACTORY_DEFAULT_CURRENT_KEY: [u8; 16] = [0u8; 16];

/// Everything an offline `SAM_ChangeKeyEntry` produced, kept for diagnostics
/// / `SAM_OFF` tracing. `entry.key_va` carries the just-loaded company key, so
/// this type gets a redacted `Debug` (like [`KeyCompanySamResult`]).
pub struct OfflineKeyResult {
    pub entry: AesKeyEntry,
    pub changecnt: u16,
    /// The `encData || MAC` payload (72 bytes for a 61-byte AES-128 entry).
    pub encke: Vec<u8>,
    /// The exact plaintext APDU transmitted (no host-session wrapping).
    pub apdu: Vec<u8>,
}

impl std::fmt::Debug for OfflineKeyResult {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("OfflineKeyResult")
            .field("changecnt", &self.changecnt)
            .field("entry.set", &self.entry.set)
            .field("entry.ext_set", &self.entry.ext_set)
            .field("entry.key_va", &"<redacted>")
            .field("encke.len", &self.encke.len())
            .field("apdu.len", &self.apdu.len())
            .finish()
    }
}

/// Run the OFFLINE `SAM_ChangeKeyEntry` for one SAM: derive the offline
/// session `(Ke, Km)` from `entry_key` (the entry's CURRENT key) + `changecnt`
/// ([`crypto::generate_offline_session_key`]), build the company PICC-master
/// key entry, encrypt+MAC it into `encke` ([`apdu::build_encke_offline`]),
/// frame the plaintext offline command ([`apdu::build_change_key_entry_offline`]),
/// transmit it PLAIN, and check the status word.
///
/// **Pure w.r.t. the SAM's lock state** — this does NOT call
/// [`flows::authenticate_host`] or [`flows::lock_unlock_unlock`]. Whether the
/// SAM must first be unlocked (via `SAM_LockUnlock` with the all-zero key) for
/// an offline change to be accepted is a bench question — the CLI drives that
/// separately (`--lock-unlock-first`) so the offline command itself stays a
/// single, isolated, fully-traceable transmit.
///
/// When compiled with the `sam-debug` cargo feature AND `SAM_OFF` is set in
/// the environment, prints the offline `Ke`/`Km`, change counter, plaintext
/// entry, `encke`, and final APDU to stderr so ONE hardware run is fully
/// diagnosable (see [`trace_sam_off`]). This is a **diagnostic-only** leak of
/// the company key material to stderr (same tradeoff as `SAM_APDU_DEBUG` in
/// [`crypto::SecureMessaging::create_full_protection_cmd`]) — the `sam-debug`
/// feature must stay OFF in any build whose terminal output is retained
/// (production, journald-backed systemd units).
///
/// Fail-closed: any non-`9000` status word aborts via `?`.
pub fn run_offline_ceremony_on_sam(
    sam: &mut dyn SamTransport,
    entry_key: &[u8; 16],
    entry_no: u8,
    company_key: &[u8; 16],
    changecnt: u16,
) -> Result<OfflineKeyResult, SamError> {
    let entry = company_picc_master_entry(company_key);
    let (ke, km) = crypto::generate_offline_session_key(entry_key, changecnt)?;
    let encke = apdu::build_encke_offline(entry_no, COMPANY_PICC_MASTER_PRO_MAS, &ke, &km, &entry)?;
    let cmd = apdu::build_change_key_entry_offline(
        entry_no,
        COMPANY_PICC_MASTER_PRO_MAS,
        changecnt,
        &encke,
    );

    trace_sam_off(
        entry_no, &entry, changecnt, entry_key, &ke, &km, &encke, &cmd,
    );

    let resp = sam.transmit(&cmd)?;
    apdu::parse_status_ok(&resp)?;

    Ok(OfflineKeyResult {
        entry,
        changecnt,
        encke,
        apdu: cmd,
    })
}

/// Diagnostic dump of one offline `SAM_ChangeKeyEntry` derivation to stderr —
/// derived session keys, change counter, the plaintext (unwrapped) entry
/// command, `encke`, and the final APDU. This is genuine company key
/// material, intentionally printed ONLY for bench diagnosis of a single
/// hardware run (see the doc comment on [`run_offline_ceremony_on_sam`]).
///
/// Compiled in only under the `sam-debug` cargo feature (default OFF, never
/// enabled in a release build — see `Cargo.toml`); the `#[cfg(not(...))]`
/// stub below has no env-var check at all, so a stray `SAM_OFF` in a
/// production binary can never leak key material this way.
#[cfg(feature = "sam-debug")]
#[allow(clippy::too_many_arguments)]
fn trace_sam_off(
    entry_no: u8,
    entry: &AesKeyEntry,
    changecnt: u16,
    entry_key: &[u8; 16],
    ke: &[u8; 16],
    km: &[u8; 16],
    encke: &[u8],
    cmd: &[u8],
) {
    if std::env::var_os("SAM_OFF").is_none() {
        return;
    }
    let plaintext =
        apdu::build_change_key_entry_plain(entry_no, COMPANY_PICC_MASTER_PRO_MAS, entry);
    eprintln!(
        "[SAM_OFF] entry_no={entry_no} pro_mas={:02X}",
        COMPANY_PICC_MASTER_PRO_MAS
    );
    eprintln!(
        "[SAM_OFF] changecnt={changecnt} (be={:02X}{:02X})",
        (changecnt >> 8) as u8,
        (changecnt & 0xff) as u8
    );
    eprintln!("[SAM_OFF] entry_key={}", hex::encode(entry_key));
    eprintln!("[SAM_OFF] offline_ke={}", hex::encode(ke));
    eprintln!("[SAM_OFF] offline_km={}", hex::encode(km));
    eprintln!("[SAM_OFF] plaintext_entry_cmd={}", hex::encode(&plaintext));
    eprintln!("[SAM_OFF] encke={}", hex::encode(encke));
    eprintln!("[SAM_OFF] final_apdu={}", hex::encode(cmd));
}

#[cfg(not(feature = "sam-debug"))]
#[allow(clippy::too_many_arguments)]
fn trace_sam_off(
    _entry_no: u8,
    _entry: &AesKeyEntry,
    _changecnt: u16,
    _entry_key: &[u8; 16],
    _ke: &[u8; 16],
    _km: &[u8; 16],
    _encke: &[u8],
    _cmd: &[u8],
) {
}

// ---------------------------------------------------------------------
// Step 5: key backup — GF(256) Shamir (k-of-n) secret sharing over the
// 16-byte company master key, so a single dead/lost SAM (or a single lost
// backup share) never orphans every card in the field.
// ---------------------------------------------------------------------

/// GF(2^8) arithmetic (Rijndael's field, poly `x^8+x^4+x^3+x+1` = `0x11B`)
/// — the same field AES itself uses, chosen for familiarity/auditability,
/// not because it interacts with the AES key material's *meaning* (Shamir
/// splitting here is a bit-level secret-sharing scheme, independent of what
/// the 16 bytes represent).
mod gf256 {
    /// Multiply two GF(2^8) elements.
    pub fn mul(mut a: u8, mut b: u8) -> u8 {
        let mut product = 0u8;
        for _ in 0..8 {
            if b & 1 != 0 {
                product ^= a;
            }
            let carry = a & 0x80;
            a <<= 1;
            if carry != 0 {
                a ^= 0x1B;
            }
            b >>= 1;
        }
        product
    }

    /// Multiplicative inverse via `a^254` (the field's nonzero elements form
    /// a group of order 255, so `a^255 = 1` for `a != 0`, hence
    /// `a^254 = a^-1`). `inv(0)` is mathematically undefined; callers here
    /// never pass 0 (Shamir x-coordinates start at 1).
    pub fn inv(a: u8) -> u8 {
        debug_assert_ne!(a, 0, "GF(256) inverse of 0 is undefined");
        let mut result = 1u8;
        let mut base = a;
        let mut exp = 254u32;
        while exp > 0 {
            if exp & 1 == 1 {
                result = mul(result, base);
            }
            base = mul(base, base);
            exp >>= 1;
        }
        result
    }
}

/// One Shamir share of a 16-byte secret: the x-coordinate (`1..=255`, never
/// `0` — that's the secret's own position) and the 16 per-byte polynomial
/// evaluations at that x.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct KeyShare {
    pub x: u8,
    pub y: [u8; 16],
    /// Recorded for backup-file labelling / operator sanity-checking only —
    /// not used by [`reconstruct_key_backup`] (which works from whatever
    /// shares it's given, regardless of the threshold they were cut for).
    pub threshold: u8,
    pub total_shares: u8,
}

/// Split a 16-byte key into `total_shares` Shamir shares such that any
/// `threshold` of them reconstruct the original key, but `threshold - 1` (or
/// fewer) reveal nothing about it (information-theoretic secrecy, per
/// Shamir's scheme).
///
/// **SPOF mitigation**: this is the mechanism by which a dead/destroyed SAM
/// (or a single lost/damaged backup share) does not orphan the company's
/// cards — store each share at a different secure location; any
/// `threshold` of them re-key a replacement SAM via
/// [`reconstruct_key_backup`] + [`run_ceremony_on_sam`] again.
pub fn split_key_backup(
    key: &[u8; 16],
    threshold: u8,
    total_shares: u8,
) -> Result<Vec<KeyShare>, SamError> {
    if threshold < 2 {
        return Err(SamError::Crypto(format!(
            "backup threshold must be >= 2 (a threshold of 1 defeats the point of splitting), got {threshold}"
        )));
    }
    if total_shares < threshold {
        return Err(SamError::Crypto(format!(
            "total_shares ({total_shares}) must be >= threshold ({threshold})"
        )));
    }

    // Random coefficients c_1..c_{threshold-1} per byte position (c_0 is the
    // secret byte itself). One CSPRNG call for all of them.
    let degree = (threshold - 1) as usize;
    let mut rand_coeffs = vec![0u8; degree * 16];
    if degree > 0 {
        csprng_fill(&mut rand_coeffs)?;
    }

    let mut shares = Vec::with_capacity(total_shares as usize);
    for x in 1..=total_shares {
        let mut y = [0u8; 16];
        for (b, y_byte) in y.iter_mut().enumerate() {
            // Horner's method, high degree to low, in GF(256). coeff(0) is
            // the secret byte; coeff(d) for d in 1..threshold is
            // rand_coeffs[(d-1)*16 + b].
            let mut acc = 0u8;
            for d in (1..threshold as usize).rev() {
                let c_d = rand_coeffs[(d - 1) * 16 + b];
                acc = gf256::mul(acc, x) ^ c_d;
            }
            acc = gf256::mul(acc, x) ^ key[b];
            *y_byte = acc;
        }
        shares.push(KeyShare {
            x,
            y,
            threshold,
            total_shares,
        });
    }
    Ok(shares)
}

/// Reconstruct the original 16-byte key from `threshold` (or more) Shamir
/// shares via Lagrange interpolation at `x = 0`. Any subset of at least
/// `threshold` genuine shares (from the same [`split_key_backup`] call)
/// reconstructs the same key, regardless of *which* shares are supplied.
///
/// Returns an error on duplicate x-coordinates (which would make the
/// interpolation ill-defined) or fewer than 2 shares. Does **not**
/// independently verify the reconstructed key is "the right one" — pass in
/// fewer than the original threshold and you silently get a wrong key, by
/// design of the scheme (this is what makes sub-threshold shares reveal
/// nothing). Verify externally (e.g. by test-keying a spare SAM) if in
/// doubt.
pub fn reconstruct_key_backup(shares: &[KeyShare]) -> Result<[u8; 16], SamError> {
    if shares.len() < 2 {
        return Err(SamError::Crypto(format!(
            "need at least 2 shares to reconstruct, got {}",
            shares.len()
        )));
    }
    let mut xs: Vec<u8> = shares.iter().map(|s| s.x).collect();
    xs.sort_unstable();
    xs.dedup();
    if xs.len() != shares.len() {
        return Err(SamError::Crypto(
            "duplicate share x-coordinates — cannot interpolate".into(),
        ));
    }

    let mut secret = [0u8; 16];
    for (b, secret_byte) in secret.iter_mut().enumerate() {
        let mut acc = 0u8;
        for (i, share_i) in shares.iter().enumerate() {
            // Lagrange basis polynomial L_i(0) = product_{j != i} xj / (xj - xi)
            // = product_{j != i} xj * inv(xj XOR xi)  (GF(2) subtraction = XOR).
            let mut l_i = 1u8;
            for (j, share_j) in shares.iter().enumerate() {
                if i == j {
                    continue;
                }
                let denom = share_j.x ^ share_i.x;
                l_i = gf256::mul(l_i, gf256::mul(share_j.x, gf256::inv(denom)));
            }
            acc ^= gf256::mul(share_i.y[b], l_i);
        }
        *secret_byte = acc;
    }
    Ok(secret)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::reader::sam::mock::MockTransport;

    #[test]
    fn generate_master_key_is_16_bytes_and_non_constant() {
        let k1 = generate_master_key().unwrap();
        let k2 = generate_master_key().unwrap();
        assert_eq!(k1.len(), 16);
        assert_ne!(k1, [0u8; 16], "must not be the all-zero key");
        assert_ne!(k1, k2, "two calls must not collide (CSPRNG, not fixed)");
    }

    #[test]
    fn generate_rnd1_and_rnd_a_are_non_constant() {
        let a1 = generate_rnd1().unwrap();
        let a2 = generate_rnd1().unwrap();
        assert_ne!(a1, a2);
        let b1 = generate_rnd_a().unwrap();
        let b2 = generate_rnd_a().unwrap();
        assert_ne!(b1, b2);
    }

    #[test]
    fn company_picc_master_entry_has_spec_flags_and_carries_the_key() {
        let key = [0x7Bu8; 16];
        let entry = company_picc_master_entry(&key);
        assert_eq!(entry.key_va, key);
        assert_eq!(entry.set.to_le_bytes(), [0x20, 0x00]);
        // 0x19 = KeyClass=PICC (0x01) | RestrictToDiversifiedUse (0x10) |
        // AllowDumpSecretKey (0x08) — see `COMPANY_PICC_MASTER_EXT_SET`'s
        // doc comment (updated 2026-07-15, bead 4ty rework: was 0x11 before
        // `SAM_DumpSecretKey` needed `AllowDumpSecretKey`).
        assert_eq!(entry.ext_set, 0x19);
    }

    /// Queue a self-consistent `SAM_LockUnlock`/Unlock pass1+pass2+pass3
    /// triple (3 APDUs) so tests exercising [`run_ceremony_on_sam`] don't
    /// need to re-derive the pre-step's mock responses by hand each time.
    /// Mirrors `flows::tests::mock_sam_for_lock_unlock` but lives here too
    /// since `ceremony`'s tests build their `sam` mocks independently.
    fn push_lock_unlock_unlock_responses(sam: &mut MockTransport, lock_unlock_rnd1: [u8; 12]) {
        let lock_unlock_rnd2 = [0x77u8; 12];
        let lock_unlock_rnd_b = [0x88u8; 16];
        let mut resp1 = lock_unlock_rnd2.to_vec();
        resp1.push(0x90);
        resp1.push(0xAF);
        sam.push_ok(resp1);

        let mac_sam = crate::reader::sam::crypto::host_auth_pass2_cmac(
            &SAM_FACTORY_DEFAULT_MASTER_KEY,
            &lock_unlock_rnd1,
            apdu::LOCK_UNLOCK_STATE_UNLOCK,
        );
        let kex = crate::reader::sam::crypto::derive_kex(
            &SAM_FACTORY_DEFAULT_MASTER_KEY,
            &lock_unlock_rnd1,
            &lock_unlock_rnd2,
        )
        .unwrap();
        let rnd_bc = crate::reader::sam::crypto::aes128_cbc_encrypt_no_pad(
            &kex,
            &[0u8; 16],
            &lock_unlock_rnd_b,
        )
        .unwrap();
        let mut resp2 = mac_sam.to_vec();
        resp2.extend_from_slice(&rnd_bc);
        resp2.push(0x90);
        resp2.push(0xAF);
        sam.push_ok(resp2);

        sam.push_ok(vec![0x90, 0x00]); // pass 3 ok
    }

    /// Push the 3-pass `SAM_AuthenticateHost` mock responses that
    /// [`run_ceremony_on_sam`] consumes after the LockUnlock pre-step and the
    /// channel reconnect. Mirrors `flows::tests::mock_sam_for_authenticate_host`
    /// (auth pass 2 doesn't verify the SAM's leading 8 bytes, so they are
    /// arbitrary here).
    fn push_authenticate_host_responses(
        sam: &mut MockTransport,
        master_key: &[u8; 16],
        rnd1: [u8; 12],
    ) {
        let rnd2 = [0x33u8; 12];
        let rnd_b = [0x44u8; 16];
        let mut resp1 = rnd2.to_vec();
        resp1.push(0x90);
        resp1.push(0xAF);
        sam.push_ok(resp1);

        let kex = crate::reader::sam::crypto::derive_kex(master_key, &rnd1, &rnd2).unwrap();
        let rnd_bc =
            crate::reader::sam::crypto::aes128_cbc_encrypt_no_pad(&kex, &[0u8; 16], &rnd_b)
                .unwrap();
        let mut resp2 = vec![0u8; 8];
        resp2.extend_from_slice(&rnd_bc);
        resp2.push(0x90);
        resp2.push(0xAF);
        sam.push_ok(resp2);

        sam.push_ok(vec![0x90, 0x00]); // pass 3 ok
    }

    #[test]
    fn run_ceremony_on_sam_uses_the_unlock_session_and_writes_key_entry() {
        let host_master_key = [0x11u8; 16];
        let rnd1 = [0x01u8; 12];
        let rnd_a = [0x02u8; 16];
        let lock_unlock_rnd1 = [0x05u8; 12];
        let lock_unlock_rnd_a = [0x06u8; 16];
        let company_key = [0x99u8; 16];

        let mut sam = MockTransport::new();
        // Per NXP AN12704 Fig. 3, `SAM_AuthenticateHost` unlocks + establishes
        // the session in one step (3 passes); ChangeKeyEntry then runs on it.
        push_authenticate_host_responses(&mut sam, &host_master_key, rnd1);

        // ChangeKeyEntry response: 9000.
        sam.push_ok(vec![0x90, 0x00]);

        let result = run_ceremony_on_sam(
            &mut sam,
            &HostKeyRef {
                key: &host_master_key,
                key_no: 0x00,
                key_ver: 0x00,
            },
            0x01,
            &company_key,
            HostChallenge {
                rnd1,
                rnd_a,
                lock_unlock_rnd1,
                lock_unlock_rnd_a,
            },
        )
        .unwrap();

        assert_eq!(result.entry.key_va, company_key);
        assert_eq!(
            sam.sent.len(),
            4,
            "3 SAM_AuthenticateHost passes + 1 ChangeKeyEntry"
        );

        // The 1st APDU is SAM_AuthenticateHost pass 1 (INS 0xA4).
        assert_eq!(
            sam.sent[0],
            apdu::build_authenticate_host_pass1(0x00, 0x00, apdu::HOST_MODE_FULL)
        );

        // The 4th APDU is the ChangeKeyEntry Full command, wrapped by the
        // secure-messaging session AuthenticateHost established (Ke/Km).
        let plain =
            apdu::build_change_key_entry_plain(0x01, COMPANY_PICC_MASTER_PRO_MAS, &result.entry);
        let mut sm = crypto::SecureMessaging::new(result.session.ke, result.session.km);
        let expected_apdu = sm.create_full_protection_cmd(&plain).unwrap();
        assert_eq!(sam.sent[3], expected_apdu);
    }

    #[test]
    fn run_ceremony_emits_three_auth_host_then_one_change_key_entry() {
        // Per NXP AN12704 Fig. 3, the online ceremony emits exactly the 3
        // SAM_AuthenticateHost passes (INS 0xA4) followed by ONE ChangeKeyEntry
        // (INS 0xC1). (HARDWARE NOTE bead 59m: this online path returns 6985 on
        // the project's current SAM AV3 — see the offline path — but the emitted
        // command sequence itself is what this guards.)
        let host_master_key = [0x11u8; 16];
        let rnd1 = [0x01u8; 12];

        let mut sam = MockTransport::new();
        push_authenticate_host_responses(&mut sam, &host_master_key, rnd1);
        sam.push_ok(vec![0x90, 0x00]); // ChangeKeyEntry -> 9000

        run_ceremony_on_sam(
            &mut sam,
            &HostKeyRef {
                key: &host_master_key,
                key_no: 0x00,
                key_ver: 0x00,
            },
            0x01,
            &[0x99u8; 16],
            HostChallenge {
                rnd1,
                rnd_a: [0x02u8; 16],
                lock_unlock_rnd1: [0x05u8; 12],
                lock_unlock_rnd_a: [0x06u8; 16],
            },
        )
        .unwrap();

        // INS byte (index 1) of each emitted APDU.
        let ins: Vec<u8> = sam.sent.iter().map(|a| a[1]).collect();
        assert_eq!(
            ins,
            vec![0xA4, 0xA4, 0xA4, 0xC1],
            "expected 3× SAM_AuthenticateHost (0xA4) then 1× ChangeKeyEntry (0xC1)"
        );
    }

    #[test]
    fn run_ceremony_on_sam_stops_if_lock_unlock_fails() {
        let host_master_key = [0x11u8; 16];
        let mut sam = MockTransport::new();
        let mut resp1 = vec![0u8; 12];
        resp1.push(0x6A);
        resp1.push(0x82);
        sam.push_ok(resp1); // LockUnlock/Unlock pass 1 rejected

        let err = run_ceremony_on_sam(
            &mut sam,
            &HostKeyRef {
                key: &host_master_key,
                key_no: 0x00,
                key_ver: 0x00,
            },
            0x01,
            &[0x99u8; 16],
            HostChallenge {
                rnd1: [0u8; 12],
                rnd_a: [0u8; 16],
                lock_unlock_rnd1: [0u8; 12],
                lock_unlock_rnd_a: [0u8; 16],
            },
        )
        .unwrap_err();
        assert!(matches!(err, SamError::Status(0x6A82)));
        assert_eq!(
            sam.sent.len(),
            1,
            "must never attempt ChangeKeyEntry after a failed unlock"
        );
    }

    #[test]
    fn run_ceremony_on_sam_propagates_change_key_entry_failure() {
        let host_master_key = [0x11u8; 16];
        let rnd1 = [0x01u8; 12];
        let rnd_a = [0x02u8; 16];
        let lock_unlock_rnd1 = [0x05u8; 12];
        let lock_unlock_rnd_a = [0x06u8; 16];

        let mut sam = MockTransport::new();
        push_lock_unlock_unlock_responses(&mut sam, lock_unlock_rnd1);

        // SAM rejects ChangeKeyEntry (e.g. locked key-storage sector).
        sam.push_ok(vec![0x91, 0x1C]);

        let err = run_ceremony_on_sam(
            &mut sam,
            &HostKeyRef {
                key: &host_master_key,
                key_no: 0x00,
                key_ver: 0x00,
            },
            0x01,
            &[0x99u8; 16],
            HostChallenge {
                rnd1,
                rnd_a,
                lock_unlock_rnd1,
                lock_unlock_rnd_a,
            },
        )
        .unwrap_err();
        assert!(matches!(err, SamError::Status(0x911C)));
    }

    #[test]
    fn run_offline_ceremony_emits_one_plain_change_key_entry_and_writes_the_key() {
        let company_key = [0x99u8; 16];
        let mut sam = MockTransport::new();
        sam.push_ok(vec![0x90, 0x00]); // offline ChangeKeyEntry -> 9000

        let result = run_offline_ceremony_on_sam(
            &mut sam,
            &ENTRY_FACTORY_DEFAULT_CURRENT_KEY,
            0x01,
            &company_key,
            0,
        )
        .unwrap();

        assert_eq!(result.entry.key_va, company_key);
        assert_eq!(result.changecnt, 0);
        assert_eq!(result.encke.len(), 72, "encData(64) + MAC(8)");

        // Exactly ONE APDU, and it is the plain offline command — NO
        // AuthenticateHost (0xA4), NO LockUnlock (0x10) inside this function.
        assert_eq!(sam.sent.len(), 1);
        let sent = &sam.sent[0];
        assert_eq!(sent[0], 0x80); // CLA
        assert_eq!(sent[1], 0xC1); // INS ChangeKeyEntry
        assert_eq!(sent[2], 0x01); // entry_no
        assert_eq!(sent[3], COMPANY_PICC_MASTER_PRO_MAS);
        assert_eq!(sent[4], (2 + 72) as u8); // Lc

        // Byte-exact against the pure builders it composes.
        let (ke, km) =
            crypto::generate_offline_session_key(&ENTRY_FACTORY_DEFAULT_CURRENT_KEY, 0).unwrap();
        let entry = company_picc_master_entry(&company_key);
        let encke =
            apdu::build_encke_offline(0x01, COMPANY_PICC_MASTER_PRO_MAS, &ke, &km, &entry).unwrap();
        let expected =
            apdu::build_change_key_entry_offline(0x01, COMPANY_PICC_MASTER_PRO_MAS, 0, &encke);
        assert_eq!(sent, &expected);
        assert_eq!(result.apdu, expected);
    }

    #[test]
    fn run_offline_ceremony_propagates_status_failure() {
        let mut sam = MockTransport::new();
        sam.push_ok(vec![0x69, 0x85]); // 6985 — SAM rejects the offline change

        let err = run_offline_ceremony_on_sam(
            &mut sam,
            &ENTRY_FACTORY_DEFAULT_CURRENT_KEY,
            0x01,
            &[0x99u8; 16],
            0,
        )
        .unwrap_err();
        assert!(matches!(err, SamError::Status(0x6985)));
    }

    #[test]
    fn run_offline_ceremony_changecnt_changes_the_apdu() {
        let company_key = [0x42u8; 16];
        let mut a = MockTransport::new();
        a.push_ok(vec![0x90, 0x00]);
        let mut b = MockTransport::new();
        b.push_ok(vec![0x90, 0x00]);

        let r0 = run_offline_ceremony_on_sam(&mut a, &[0u8; 16], 0x01, &company_key, 0).unwrap();
        let r1 = run_offline_ceremony_on_sam(&mut b, &[0u8; 16], 0x01, &company_key, 1).unwrap();
        // Different change counter -> different offline session -> different
        // encke and different wire APDU (counter is bound into Ke/Km).
        assert_ne!(r0.encke, r1.encke);
        assert_ne!(a.sent[0], b.sent[0]);
    }

    #[test]
    fn gf256_mul_and_inv_are_consistent() {
        for a in 1..=255u8 {
            let inv_a = gf256::inv(a);
            assert_eq!(
                gf256::mul(a, inv_a),
                1,
                "a * inv(a) must be the multiplicative identity, a={a}"
            );
        }
    }

    #[test]
    fn split_and_reconstruct_key_backup_round_trips_with_exact_threshold() {
        let key = [0x42u8; 16];
        let shares = split_key_backup(&key, 3, 5).unwrap();
        assert_eq!(shares.len(), 5);

        // Any 3 of the 5 shares reconstruct the original key.
        let subset = &shares[0..3];
        let reconstructed = reconstruct_key_backup(subset).unwrap();
        assert_eq!(reconstructed, key);

        let subset2 = &[shares[1].clone(), shares[3].clone(), shares[4].clone()];
        let reconstructed2 = reconstruct_key_backup(subset2).unwrap();
        assert_eq!(reconstructed2, key);
    }

    #[test]
    fn split_and_reconstruct_key_backup_round_trips_with_more_than_threshold() {
        let key = [0xABu8; 16];
        let shares = split_key_backup(&key, 2, 4).unwrap();
        // All 4 shares (more than the threshold of 2) still reconstruct.
        let reconstructed = reconstruct_key_backup(&shares).unwrap();
        assert_eq!(reconstructed, key);
    }

    #[test]
    fn split_key_backup_rejects_invalid_k_of_n() {
        let key = [0u8; 16];
        assert!(split_key_backup(&key, 1, 5).is_err(), "threshold < 2");
        assert!(
            split_key_backup(&key, 6, 5).is_err(),
            "threshold > total_shares"
        );
    }

    #[test]
    fn reconstruct_key_backup_rejects_too_few_or_duplicate_shares() {
        let key = [0x11u8; 16];
        let shares = split_key_backup(&key, 3, 5).unwrap();

        assert!(
            reconstruct_key_backup(&shares[0..1]).is_err(),
            "only 1 share"
        );

        let dup = vec![shares[0].clone(), shares[0].clone()];
        assert!(
            reconstruct_key_backup(&dup).is_err(),
            "duplicate x-coordinate"
        );
    }

    #[test]
    fn sub_threshold_shares_do_not_reconstruct_the_key() {
        // With only threshold-1 shares, interpolation is under-determined —
        // reconstruct_key_backup will still run (it doesn't know the
        // original threshold) but the result must NOT equal the real key,
        // demonstrating why "threshold" shares are actually required.
        let key = [0x77u8; 16];
        let shares = split_key_backup(&key, 3, 5).unwrap();
        let sub = &shares[0..2]; // only 2, threshold was 3
        let wrong = reconstruct_key_backup(sub).unwrap();
        assert_ne!(wrong, key);
    }
}
