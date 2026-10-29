//! SAM/PICC transport abstractions.
//!
//! The pure APDU builders in [`super::apdu`] never touch hardware; a
//! [`SamTransport`] (SAM slot) and, for the card-native EV2 handshake
//! frames, a [`PiccTransport`] (PICC/contactless slot) are the only I/O
//! seams. Flows built on these traits (see [`super::flows`]) are therefore
//! testable against the in-memory mock in [`super::mock`] with no PC/SC
//! daemon or hardware present.

use super::error::SamError;

/// Sends a raw APDU to the SAM and returns its raw response (data + SW1SW2).
pub trait SamTransport {
    fn transmit(&mut self, apdu: &[u8]) -> Result<Vec<u8>, SamError>;

    /// Warm-reset the SAM channel: keep the card powered (persistent config
    /// such as the lock/unlock state survives) but clear the volatile
    /// per-channel session state — specifically the SAM's "this channel has
    /// done its one host authentication" flag.
    ///
    /// Needed by the key ceremony: `SAM_LockUnlock`/Unlock both unlocks the
    /// SAM (persistent) AND consumes this channel's single allowed host
    /// authentication, so a subsequent `SAM_AuthenticateHost` on the SAME
    /// channel is refused with `6985`. Reconnecting between the two lets the
    /// now-unlocked SAM accept a fresh `AuthenticateHost` (which is the only
    /// path that establishes the secure-messaging session `ChangeKeyEntry`
    /// needs — `LockUnlock` alone establishes none, per liblogicalaccess).
    ///
    /// Default: no-op (the in-memory mock has no channel to reset).
    fn reconnect(&mut self) -> Result<(), SamError> {
        Ok(())
    }
}

/// Sends a raw native DESFire frame to the PICC (contactless card) and
/// returns its raw response.
pub trait PiccTransport {
    fn transceive(&mut self, frame: &[u8]) -> Result<Vec<u8>, SamError>;
}

/// Trace an outgoing SAM/PICC APDU to stderr — compiled in only under the
/// `sam-debug` cargo feature (default OFF, must never be on in a release
/// build), and only prints when `SAM_APDU_DEBUG` is also set at runtime. The
/// `#[cfg(not(...))]` stub below has no env-var check at all, so a stray
/// `SAM_APDU_DEBUG` in a production binary can never dump an APDU (which may
/// carry key material in cleartext, e.g. the offline `ChangeKeyEntry`
/// command) to stderr/journald. Only used by [`pcsc_impl`] (the `real-reader`
/// build), hence the outer `real-reader` gate too — no point compiling a
/// no-op trace helper into a mock-only binary.
#[cfg(feature = "real-reader")]
#[cfg(feature = "sam-debug")]
fn trace_out(label: &str, apdu: &[u8]) {
    if std::env::var_os("SAM_APDU_DEBUG").is_some() {
        eprintln!("[{label} >] {}", hex::encode(apdu));
    }
}

#[cfg(feature = "real-reader")]
#[cfg(not(feature = "sam-debug"))]
fn trace_out(_label: &str, _apdu: &[u8]) {}

/// Trace an incoming SAM/PICC response to stderr — see [`trace_out`].
#[cfg(feature = "real-reader")]
#[cfg(feature = "sam-debug")]
fn trace_in(label: &str, resp: &[u8]) {
    if std::env::var_os("SAM_APDU_DEBUG").is_some() {
        eprintln!("[{label} <] {}", hex::encode(resp));
    }
}

#[cfg(feature = "real-reader")]
#[cfg(not(feature = "sam-debug"))]
fn trace_in(_label: &str, _resp: &[u8]) {}

#[cfg(feature = "real-reader")]
mod pcsc_impl {
    use std::ffi::CString;

    use pcsc::{Card, Context, Disposition, Protocols, ShareMode, MAX_BUFFER_SIZE};

    use super::{trace_in, trace_out, PiccTransport, SamError, SamTransport};

    /// Real PC/SC-backed SAM transport: opens the SAM card slot and forwards
    /// every APDU via `Card::transmit`.
    pub struct SamPcscTransport {
        card: Card,
    }

    impl SamPcscTransport {
        /// Connect to the first reader whose name matches [`find_sam_reader`].
        pub fn open(ctx: &Context) -> Result<Self, SamError> {
            let name = find_sam_reader(ctx).ok_or(SamError::NoReader("*SAM*"))?;
            // T=1 explicitly: the SAM AV3 returns 12-byte challenges with SW
            // 0x90AF; under T=0 (which Protocols::ANY may negotiate) that
            // case-4 exchange is mangled to a 2-byte reply. Confirmed against
            // real hardware (scriptor uses T=1 and succeeds). See bead 59m.
            let card = ctx
                .connect(&name, ShareMode::Shared, Protocols::T1)
                .map_err(|e| SamError::Transport(e.to_string()))?;
            Ok(Self { card })
        }

        /// Connect to an explicitly named reader (bypassing enumeration —
        /// useful when the caller already knows the reader name, e.g. from
        /// config).
        pub fn open_named(ctx: &Context, reader_name: &CString) -> Result<Self, SamError> {
            // T=1 explicitly (see open() — SAM 0x90AF exchange breaks under T=0).
            let card = ctx
                .connect(reader_name, ShareMode::Shared, Protocols::T1)
                .map_err(|e| SamError::Transport(e.to_string()))?;
            Ok(Self { card })
        }
    }

    impl SamTransport for SamPcscTransport {
        fn transmit(&mut self, apdu: &[u8]) -> Result<Vec<u8>, SamError> {
            let mut buf = [0u8; MAX_BUFFER_SIZE];
            trace_out("SAM", apdu);
            let resp = self
                .card
                .transmit(apdu, &mut buf)
                .map_err(|e| SamError::Transport(e.to_string()))?;
            trace_in("SAM", resp);
            Ok(resp.to_vec())
        }

        /// Warm reset (`Disposition::ResetCard`) on the same T=1 SAM channel —
        /// see the trait doc. Persistent state (the unlock) survives; the
        /// per-channel host-auth flag clears so `AuthenticateHost` can run.
        fn reconnect(&mut self) -> Result<(), SamError> {
            self.card
                .reconnect(ShareMode::Shared, Protocols::T1, Disposition::ResetCard)
                .map_err(|e| SamError::Transport(format!("SAM channel reconnect failed: {e}")))
        }
    }

    /// Real PC/SC-backed PICC transport (the contactless card in the PICC
    /// slot), used for the DESFire-native frames in the EV2 handshake.
    ///
    /// HARDWARE-VERIFY (see also `apdu::build_picc_authenticate_ev2_first`):
    /// `AuthenticateEV2First` and its continuation frames are native
    /// ISO14443-4 frames, not ISO 7816-4 case-2/3/4 APDUs. Whether
    /// `Card::transmit` on this reader passes such frames through unmodified
    /// (assumed here) or requires a different escape/control code is
    /// unconfirmed until tested against real hardware.
    pub struct PiccPcscTransport {
        card: Card,
    }

    impl PiccPcscTransport {
        pub fn open_named(ctx: &Context, reader_name: &CString) -> Result<Self, SamError> {
            let card = ctx
                .connect(reader_name, ShareMode::Shared, Protocols::ANY)
                .map_err(|e| SamError::Transport(e.to_string()))?;
            Ok(Self { card })
        }

        /// Read the card's UID via the PC/SC pseudo-APDU `FF CA 00 00 00`
        /// (Get Data — UID), on the same connected `Card` used for the
        /// subsequent EV2 handshake/read frames.
        ///
        /// **Used ONLY to derive the SAM diversification input** for
        /// `sam::punch::authenticate_and_read_token_id` (bead bi0) — a UID is
        /// a readable-in-the-clear, copyable "barcode", never proof of
        /// authenticity on its own. Callers must NEVER treat this return
        /// value as, or emit it as, an authenticated identity; see the
        /// architecture doc §1/§3b and `sam::punch`'s module doc.
        ///
        /// Requires the UID to be exactly 7 bytes (this project's DESFire
        /// EV2 UID length assumption — see `crypto::diversification_input`);
        /// any other length is a hard [`SamError::Transport`] (fail-closed —
        /// mirrors `sam_personalize.rs`'s `wait_for_card_and_read_uid`).
        pub fn read_uid(&self) -> Result<[u8; 7], SamError> {
            const GET_UID: [u8; 5] = [0xFF, 0xCA, 0x00, 0x00, 0x00];
            let mut buf = [0u8; MAX_BUFFER_SIZE];
            let resp = self
                .card
                .transmit(&GET_UID, &mut buf)
                .map_err(|e| SamError::Transport(e.to_string()))?;
            let uid_bytes = if resp.len() >= 2 {
                &resp[..resp.len() - 2]
            } else {
                resp
            };
            if uid_bytes.len() != 7 {
                return Err(SamError::Transport(format!(
                    "expected a 7-byte DESFire UID, got {} bytes ({}) — is this a personalized \
                     DESFire EV2 fob in the PICC slot?",
                    uid_bytes.len(),
                    hex::encode(uid_bytes)
                )));
            }
            let mut uid = [0u8; 7];
            uid.copy_from_slice(uid_bytes);
            Ok(uid)
        }
    }

    impl PiccTransport for PiccPcscTransport {
        fn transceive(&mut self, frame: &[u8]) -> Result<Vec<u8>, SamError> {
            let mut buf = [0u8; MAX_BUFFER_SIZE];
            trace_out("PICC", frame);
            let resp = self
                .card
                .transmit(frame, &mut buf)
                .map_err(|e| SamError::Transport(e.to_string()))?;
            trace_in("PICC", resp);
            Ok(resp.to_vec())
        }
    }

    /// Find the first reader whose name contains "SAM" (case-insensitive).
    ///
    /// HARDWARE-VERIFY: the ACR1252U's SAM-slot reader name is not literally
    /// documented anywhere we have access to; the PICC reader name is known
    /// (`ACS ACR1252 1S CL Reader PICC 0`, see `pcsc_reader.rs`), but the SAM
    /// slot's exact PC/SC name has not been confirmed against real hardware.
    /// The ACR1252U API doc's ACOS6-SAM opcode reference is for a *different*
    /// SAM product and must not be used to guess this name.
    fn find_sam_reader(ctx: &Context) -> Option<CString> {
        find_reader_by_name_substring(ctx, "SAM")
    }

    /// Find the first PC/SC reader whose name contains `needle`
    /// (case-insensitive). Shared by [`find_sam_reader`] (fixed "SAM"
    /// needle) and the key-ceremony CLI (`src/bin/sam_ceremony.rs`), which
    /// lets a technician target a specific reader by an operator-supplied
    /// substring when a site has more than one SAM slot (terminal + backup).
    pub fn find_reader_by_name_substring(ctx: &Context, needle: &str) -> Option<CString> {
        let mut buf = vec![0u8; 2048];
        let readers = ctx.list_readers(&mut buf).ok()?;
        let needle_upper = needle.to_uppercase();
        for name in readers {
            if name
                .to_string_lossy()
                .to_uppercase()
                .contains(&needle_upper)
            {
                return Some(name.to_owned());
            }
        }
        None
    }
}

#[cfg(feature = "real-reader")]
pub use pcsc_impl::{find_reader_by_name_substring, PiccPcscTransport, SamPcscTransport};
