//! In-memory mock transport for unit-testing SAM/PICC flows without any
//! PC/SC daemon or hardware present. Compiled unconditionally (not just
//! under `#[cfg(test)]`) so later beads (ceremony/personalization/kiosk-auth)
//! can reuse it in their own tests too.

use std::collections::VecDeque;

use super::error::SamError;
use super::transport::{PiccTransport, SamTransport};

/// A transport that plays back a fixed queue of canned responses and
/// records every APDU/frame it was sent, so tests can assert both the exact
/// emitted traffic and the resulting parse.
///
/// Implements both [`SamTransport`] and [`PiccTransport`] so a single mock
/// type covers both roles in a test (construct one instance per role).
#[derive(Default)]
pub struct MockTransport {
    responses: VecDeque<SamResult>,
    /// Every APDU/frame passed to `transmit`/`transceive`, in call order.
    pub sent: Vec<Vec<u8>>,
}

type SamResult = Result<Vec<u8>, SamError>;

impl MockTransport {
    pub fn new() -> Self {
        Self::default()
    }

    /// Queue a successful response.
    pub fn push_ok(&mut self, resp: impl Into<Vec<u8>>) -> &mut Self {
        self.responses.push_back(Ok(resp.into()));
        self
    }

    /// Queue a transport-level failure (e.g. simulating a reader I/O error).
    pub fn push_err(&mut self, err: SamError) -> &mut Self {
        self.responses.push_back(Err(err));
        self
    }

    fn next(&mut self, apdu: &[u8]) -> SamResult {
        self.sent.push(apdu.to_vec());
        self.responses
            .pop_front()
            .unwrap_or_else(|| Err(SamError::Transport("mock: no response queued".into())))
    }
}

impl SamTransport for MockTransport {
    fn transmit(&mut self, apdu: &[u8]) -> Result<Vec<u8>, SamError> {
        self.next(apdu)
    }
}

impl PiccTransport for MockTransport {
    fn transceive(&mut self, frame: &[u8]) -> Result<Vec<u8>, SamError> {
        self.next(frame)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn records_sent_apdus_and_plays_back_queued_responses_in_order() {
        let mut mock = MockTransport::new();
        mock.push_ok(vec![0x90, 0x00]);
        mock.push_ok(vec![0x11, 0x22]);

        let r1 = SamTransport::transmit(&mut mock, &[0x80, 0xA4]).unwrap();
        let r2 = SamTransport::transmit(&mut mock, &[0x80, 0x0A]).unwrap();

        assert_eq!(r1, vec![0x90, 0x00]);
        assert_eq!(r2, vec![0x11, 0x22]);
        assert_eq!(mock.sent, vec![vec![0x80, 0xA4], vec![0x80, 0x0A]]);
    }

    #[test]
    fn missing_queued_response_is_a_transport_error_not_a_panic() {
        let mut mock = MockTransport::new();
        let err = SamTransport::transmit(&mut mock, &[0x00]).unwrap_err();
        assert!(matches!(err, SamError::Transport(_)));
    }

    #[test]
    fn queued_error_is_returned_as_is() {
        let mut mock = MockTransport::new();
        mock.push_err(SamError::Status(0x6A82));
        let err = SamTransport::transmit(&mut mock, &[0x00]).unwrap_err();
        assert!(matches!(err, SamError::Status(0x6A82)));
    }
}
