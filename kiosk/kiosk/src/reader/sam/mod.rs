//! SAM AV3 (S-mode, host-mediated) core module.
//!
//! Byte-accurate APDU builders/response parsers ([`apdu`]), the AES-128
//! crypto primitives they need ([`crypto`]: CMAC per RFC 4493, AN10922 key
//! diversification, `SAM_AuthenticateHost` session-key derivation), a narrow
//! transport seam ([`transport`]: [`SamTransport`] / [`PiccTransport`]) with
//! a mock ([`mock`]) for hardware-free testing, two transport-agnostic
//! protocol flows ([`flows::authenticate_picc_ev2`],
//! [`flows::authenticate_host`]), and the key-loading ceremony
//! ([`ceremony`]) built on top of them.
//!
//! **Scope of this module**: the SAM AV3 core, the key-loading ceremony
//! (bead 3pn), fob personalization (bead 4ty), and the kiosk's daily
//! punch-time authentication ([`punch`], bead bi0).
//!
//! **Fail-closed, always**: every function here returns [`SamError`] on any
//! unexpected status word, status byte, or response shape. Callers must
//! never fall back to trusting a bare card UID when a [`SamError`] occurs.
//!
//! See the architecture doc (`docs/architektur-nfc-sam.md`, Kap. 9) for the
//! full protocol rationale; several details below are marked
//! `HARDWARE-VERIFY` because they cannot be confirmed without a real SAM
//! AV3 + DESFire card on the bench.

pub mod apdu;
pub mod ceremony;
pub mod crypto;
pub mod error;
pub mod flows;
pub mod mock;
pub mod personalize;
pub mod punch;
pub mod transport;

// Convenience re-exports forming this module's public API surface. The
// key-loading ceremony (`ceremony`, `src/bin/sam_ceremony.rs`), fob
// personalization (`personalize`, `src/bin/sam_personalize.rs`), and the
// kiosk's daily punch-time authentication (`punch`, `reader::pcsc_reader`,
// real-reader gated) are the real callers of most of these; a couple of
// items are unused from a given (non-real-reader) binary's point of view —
// expected, not a real dead-code issue.
#[allow(unused_imports)]
pub use error::SamError;
#[allow(unused_imports)]
pub use flows::{
    authenticate_host, authenticate_picc_ev2, authenticate_picc_ev2_diversified,
    authenticate_picc_ev2_software, dump_secret_key, read_token_id, read_token_id_software,
    send_via_sm, Ev2AuthResult, Ev2SoftwareAuthResult,
};
#[allow(unused_imports)]
pub use personalize::{
    default_personalize_config, generate_personalize_challenge, generate_token_id, personalize_fob,
    FobKeys, PersonalizeChallenge, PersonalizeConfig, PersonalizedFob,
};
#[allow(unused_imports)]
pub use punch::{
    authenticate_and_read_token_id, generate_punch_challenge, PunchAuthConfig, PunchChallenge,
};
#[allow(unused_imports)]
pub use transport::{PiccTransport, SamTransport};

#[cfg(feature = "real-reader")]
#[allow(unused_imports)]
pub use transport::{find_reader_by_name_substring, PiccPcscTransport, SamPcscTransport};
