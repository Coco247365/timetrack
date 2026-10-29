//! Mock card reader: simulates taps from stdin so the kiosk runs with no
//! hardware on a dev box.

use std::io::{self, BufRead, Write};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::Sender;
use std::sync::Arc;

use super::{CardEvent, CardReader};

/// Reads lines from stdin and turns them into [`CardEvent`]s.
///
/// - A non-empty trimmed line → [`CardEvent::Detected`] with that text as
///   `token` and a synthetic hex chip UID (bead rst — mirrors the real
///   reader carrying both alongside each other), UNLESS it is one of the two
///   terminal-personalize-mode conventions below AND `personalize_flag` is
///   currently set (mirrors real hardware, where these outcomes only arise
///   while personalize mode is active):
///   - `blank` (case-insensitive) → [`CardEvent::Personalized`] with a
///     synthetic hex token-id and a synthetic hex chip UID (simulates a
///     successful blank-fob personalization — no real crypto runs).
///   - `foreign` (case-insensitive) → [`CardEvent::PersonalizeRejected`]
///     (simulates a card that failed both normal auth and personalization).
/// - An empty line or the literal `x` → [`CardEvent::Removed`].
///
/// Outside personalize mode `blank`/`foreign` are typed exactly like any
/// other badge id (plain `CardEvent::Detected`) — unchanged from before this
/// feature existed.
pub struct MockReader {
    personalize_flag: Arc<AtomicBool>,
}

impl MockReader {
    /// Construct a new mock reader. `personalize_flag` mirrors terminal
    /// personalize mode (see `main.rs::sync_personalize_flag`) — the real
    /// PC/SC reader takes the same parameter for the same reason.
    #[must_use]
    pub fn new(personalize_flag: Arc<AtomicBool>) -> Self {
        Self { personalize_flag }
    }
}

impl CardReader for MockReader {
    fn run(self: Box<Self>, tx: Sender<CardEvent>) {
        // One-line usage hint on the controlling terminal.
        let mut stdout = io::stdout();
        let _ = writeln!(
            stdout,
            "MOCK reader: type a badge id (e.g. NFC005) + Enter to simulate a tap; 'x' = remove. \
             In personalize mode: 'blank' = simulate a successful blank-fob personalization, \
             'foreign' = simulate a rejected/foreign card."
        );
        let _ = stdout.flush();

        let stdin = io::stdin();
        let mut line = String::new();
        loop {
            line.clear();
            match stdin.lock().read_line(&mut line) {
                // EOF (stdin closed): stop the loop.
                Ok(0) => break,
                Ok(_) => {
                    let trimmed = line.trim();
                    let personalizing = self.personalize_flag.load(Ordering::Relaxed);
                    let event = if trimmed.is_empty() || trimmed.eq_ignore_ascii_case("x") {
                        CardEvent::Removed
                    } else if personalizing && trimmed.eq_ignore_ascii_case("blank") {
                        CardEvent::Personalized {
                            token: mock_token_id_hex(),
                            uid: mock_uid_hex(),
                        }
                    } else if personalizing && trimmed.eq_ignore_ascii_case("foreign") {
                        CardEvent::PersonalizeRejected
                    } else {
                        CardEvent::Detected {
                            token: trimmed.to_owned(),
                            uid: mock_uid_hex(),
                        }
                    };
                    // If the UI side has gone away, exit cleanly.
                    if tx.send(event).is_err() {
                        break;
                    }
                }
                // A read error (e.g. detached stdin) ends the loop rather than
                // spinning.
                Err(_) => break,
            }
        }
    }
}

/// A synthetic 32-hex-char token-id for the mock reader's `blank` convention
/// — distinct on every call so a simulated batch doesn't collide with itself
/// (mirrors [`super::sam::personalize::generate_token_id`]'s CSPRNG source,
/// but doesn't touch the real SAM crypto core — this is dev-only simulation).
fn mock_token_id_hex() -> String {
    let mut id = [0u8; 16];
    // getrandom is already a dependency (real personalization uses it) —
    // reuse it here rather than adding a second RNG source for a dev-only
    // helper.
    let _ = getrandom::getrandom(&mut id);
    hex::encode(id)
}

/// A synthetic 14-hex-char (7-byte) chip UID for the mock reader — mirrors
/// the real DESFire EV2 UID length (`sam::transport::read_uid` returns
/// `[u8; 7]`), distinct on every call for the same reason as
/// [`mock_token_id_hex`] (bead rst — the mock reader now carries a UID
/// alongside every `Detected`/`Personalized` event, same as real hardware).
fn mock_uid_hex() -> String {
    let mut id = [0u8; 7];
    let _ = getrandom::getrandom(&mut id);
    hex::encode(id)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn mock_token_id_hex_is_32_chars_and_varies() {
        let a = mock_token_id_hex();
        let b = mock_token_id_hex();
        assert_eq!(a.len(), 32);
        assert_ne!(a, b);
    }

    #[test]
    fn mock_uid_hex_is_14_chars_and_varies() {
        let a = mock_uid_hex();
        let b = mock_uid_hex();
        assert_eq!(a.len(), 14);
        assert_ne!(a, b);
    }
}
