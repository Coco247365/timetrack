//! Card reader abstraction.
//!
//! A reader runs a blocking loop on its own thread and emits [`CardEvent`]s
//! over an [`std::sync::mpsc::Sender`]. The concrete reader (mock or PC/SC) is
//! chosen at startup behind the [`CardReader`] trait, so the rest of the app
//! never depends on whether real hardware is present.

use std::sync::mpsc::Sender;

pub mod mock;
pub mod sam;

#[cfg(feature = "real-reader")]
pub mod pcsc_reader;

/// Events produced by a card reader.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CardEvent {
    /// A card with the given identifier was presented.
    ///
    /// [`mock::MockReader`] emits whatever badge id the operator typed
    /// (e.g. `NFC005`) alongside a synthetic `uid`. [`pcsc_reader::PcscReader`]
    /// (`real-reader` feature, bead bi0) emits the **authenticated token-id**
    /// (hex, `token`) after a successful SAM-mediated DESFire EV2
    /// authentication — never the card's bare UID as the identity; see
    /// `sam::punch`'s module doc for why (a UID is a copyable "barcode", not
    /// proof of authenticity). The card's chip UID (hex) is carried
    /// separately as `uid` — used ONLY so the caller can (bead rst) send it
    /// to `POST /nfc/register-to-pool` for backfill on an unassigned/unknown
    /// badge (HTTP 404), never as a credential or identity in its own right.
    /// A failed authentication emits **no** event at all (fail-closed, bead
    /// cqo), UNLESS terminal personalize mode is active — see
    /// [`Self::Personalized`] / [`Self::PersonalizeRejected`] below.
    Detected {
        /// The authenticated token-id (hex) — the card's identity.
        token: String,
        /// The card's chip UID (hex) — diversification input, never a
        /// credential; carried so the caller can register it with the
        /// backend pool (bead rst).
        uid: String,
    },
    /// The card was removed from the field.
    Removed,
    /// Terminal personalize mode (admin-fob-gated batch fob personalization,
    /// see [`crate::personalize_mode`]): a tap failed normal SAM/EV2
    /// authentication, but the reader — ONLY because its own personalize-mode
    /// flag was set at the time — attempted to personalize the card as a
    /// blank DESFire EV2 fob (reusing [`sam::personalize::personalize_fob`]
    /// unchanged) and that succeeded. Carries the freshly written token-id
    /// (hex, `token`) and the card's chip UID (hex, `uid`), which the caller
    /// must still register with the backend (`POST /nfc/register-to-pool`,
    /// both fields, bead rst) before counting it as done. Never emitted
    /// while personalize mode is off — an auth failure outside personalize
    /// mode still emits nothing at all, unchanged.
    Personalized {
        /// The freshly written token-id (hex).
        token: String,
        /// The fob's chip UID (hex).
        uid: String,
    },
    /// Terminal personalize mode: a tap failed BOTH normal SAM/EV2 auth AND
    /// blank-fob personalization (a foreign/non-DESFire/damaged card). The UI
    /// should show a brief "kein leerer Chip" hint and remain in personalize
    /// mode. Like [`Self::Personalized`], only ever emitted while personalize
    /// mode is active.
    PersonalizeRejected,
}

/// A card reader that blocks on its own thread and sends [`CardEvent`]s.
///
/// Implementors take ownership of `self` so the reader can hold non-`Sync`
/// resources (e.g. a PC/SC context) confined to its thread.
pub trait CardReader: Send {
    /// Run the blocking read loop until the channel receiver is dropped (the
    /// loop should exit cleanly once `tx.send` returns `Err`).
    fn run(self: Box<Self>, tx: Sender<CardEvent>);
}
