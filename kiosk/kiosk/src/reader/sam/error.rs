//! Errors for the SAM AV3 core module.
//!
//! Every variant here is a **hard reject**: callers must fail closed on any
//! `SamError` — never fall back to trusting a bare card UID. See
//! `CLAUDE.md` / the NFC-SAM architecture doc for why (BetrVG-adjacent
//! access-control requirement: a punch must be provably tied to a
//! cryptographically authenticated card, not just "a UID was read").

use thiserror::Error;

/// Errors from SAM/PICC transport I/O or APDU parsing.
#[derive(Debug, Error)]
pub enum SamError {
    /// The underlying transport (PC/SC `SCardTransmit`, or the mock) failed.
    #[error("SAM/PICC transport error: {0}")]
    Transport(String),

    /// No PC/SC reader matched the expected name pattern. Only constructed
    /// under the `real-reader` feature (see `transport::pcsc_impl`); the
    /// default (mock-only) build never triggers it, hence the `dead_code`
    /// allow there.
    #[error("no PC/SC reader matching {0} found")]
    #[cfg_attr(not(feature = "real-reader"), allow(dead_code))]
    NoReader(&'static str),

    /// The SAM returned a status word other than `9000` (or, where a
    /// continuation is expected, other than `90AF`).
    #[error("SAM returned non-success status word {0:04X}")]
    Status(u16),

    /// The PICC (card) returned a native status byte other than the one
    /// expected for this step (`0x00` = OK, `0xAF` = additional frame).
    #[error("PICC returned unexpected native status byte {0:02X}")]
    PiccStatus(u8),

    /// A response had the wrong length to be parsed as the expected frame.
    #[error("unexpected response length: got {got}, expected {expected}")]
    UnexpectedLength { got: usize, expected: usize },

    /// A crypto primitive was called with invalid input (bad key/IV/message
    /// length) or a derivation failed.
    #[error("crypto error: {0}")]
    Crypto(String),

    /// A CMAC the caller computed did not match the CMAC the SAM returned —
    /// used by `SAM_LockUnlock` pass 2's mutual-auth check (see
    /// `flows::lock_unlock_unlock`). Fail-closed like every other variant
    /// here: never treat the SAM as unlocked/authenticated on a mismatch.
    #[error("CMAC verification failed: {0}")]
    CmacMismatch(String),
}
