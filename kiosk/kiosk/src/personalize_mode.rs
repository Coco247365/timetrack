//! Terminal personalize-mode state machine (admin-fob-gated batch fob
//! personalization).
//!
//! An **admin fob**, tapped at the wall terminal, unlocks a **personalize
//! mode** in which the operator taps blank DESFire fobs to personalize them
//! in batch, complementing the existing SSH CLI (`sam_personalize`). This
//! module is the pure, hardware-free decision core: it only deals in
//! abstract [`TapKind`]/[`ModeEvent`] inputs and [`ModeAction`] outputs, so
//! every transition is unit-testable with no reader/backend involved.
//!
//! The caller (`main.rs`) is responsible for:
//! - classifying a resolved tap into a [`TapKind`] — `Admin`/`Employee` come
//!   from the backend's `is_admin` preview flag (see `api::PreviewResult`),
//!   `BlankPersonalized`/`Foreign` come from
//!   [`crate::reader::CardEvent::Personalized`] /
//!   [`crate::reader::CardEvent::PersonalizeRejected`] (only emitted by the
//!   reader while its own personalize-mode flag mirrors this module's
//!   [`Mode`] — see `main.rs`'s mode-flag sync);
//! - driving the Slint UI (heading/message/count/hint) and timers from the
//!   returned [`ModeAction`].
//!
//! Exit triggers (per spec): (1) the "Beenden" button, (2) 5 minutes of
//! inactivity (no tap), (3) a regular employee fob tap — which must ALSO
//! still be processed as a normal punch, since personalize mode must never
//! block an employee from clocking in/out. Re-tapping the SAME admin fob
//! toggles the mode back off.

/// Which of the four tap categories a resolved tap fell into.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TapKind {
    /// Backend `is_admin: true` (see `api::PreviewResult`).
    Admin,
    /// SAM-authenticated and resolves to an assigned employee (normal punch
    /// flow) — `is_admin: false`.
    Employee,
    /// A blank DESFire fob was successfully personalized during personalize
    /// mode (`CardEvent::Personalized`).
    BlankPersonalized,
    /// Neither SAM-authenticated as an employee/admin fob NOR successfully
    /// personalizable as blank — a foreign/non-DESFire/damaged card
    /// (`CardEvent::PersonalizeRejected`). Only reachable while personalize
    /// mode is active: the reader fails closed and silent (emits nothing)
    /// on an auth failure outside personalize mode, exactly as before this
    /// feature existed.
    Foreign,
}

/// Inputs the state machine reacts to.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ModeEvent {
    Tap(TapKind),
    BeendenPressed,
    Timeout,
}

/// Terminal mode. `count` is the running "fobs personalized" tally for the
/// current personalize-mode session — reset to `0` every time mode is
/// (re-)entered.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum Mode {
    #[default]
    Normal,
    Personalize {
        count: u32,
    },
}

impl Mode {
    /// Whether personalize mode is currently active.
    #[must_use]
    pub fn is_personalize(self) -> bool {
        matches!(self, Self::Personalize { .. })
    }

    /// The running personalized-fob count, or `0` outside personalize mode.
    #[must_use]
    pub fn count(self) -> u32 {
        match self {
            Self::Normal => 0,
            Self::Personalize { count } => count,
        }
    }
}

/// What the caller must do in response to a [`transition`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ModeAction {
    /// Not in personalize mode, tap resolved to an employee — process the
    /// normal preview/menu punch flow exactly as before this feature
    /// existed.
    ProcessNormalPunch,
    /// Admin tap while `Normal` — entered personalize mode (count reset to
    /// `0`); show the personalize screen.
    EnterPersonalize,
    /// Admin tap while already `Personalize` — toggled back off; return to
    /// idle.
    ExitPersonalize,
    /// Employee tap while `Personalize` — mode exits AND the tap must still
    /// be processed as a normal punch (an employee must never be blocked by
    /// personalize mode being active).
    ExitPersonalizeAndPunch,
    /// Blank fob personalized while `Personalize` — stay in mode; the new
    /// count is `Mode::count()` on the returned mode.
    IncrementCount,
    /// Foreign/failed tap while `Personalize` — stay in mode, show a brief
    /// "kein leerer Chip" hint; count unchanged.
    ShowForeignHint,
    /// "Beenden" pressed, or the 5-minute inactivity timeout fired — mode
    /// exits silently (no punch to process). A no-op if already `Normal`
    /// (idempotent — e.g. a stale timer firing after a manual exit).
    ExitSilently,
}

/// Advance the state machine by one event. Returns the new [`Mode`] and the
/// [`ModeAction`] the caller must perform.
///
/// Pure: no I/O, no hardware, no backend calls — fully deterministic given
/// `(mode, event)`, which is what makes every transition below directly
/// unit-testable.
#[must_use]
pub fn transition(mode: Mode, event: ModeEvent) -> (Mode, ModeAction) {
    match (mode, event) {
        // -- Normal mode ----------------------------------------------------
        (Mode::Normal, ModeEvent::Tap(TapKind::Admin)) => {
            (Mode::Personalize { count: 0 }, ModeAction::EnterPersonalize)
        }
        (Mode::Normal, ModeEvent::Tap(TapKind::Employee)) => {
            (Mode::Normal, ModeAction::ProcessNormalPunch)
        }
        // BlankPersonalized/Foreign cannot occur outside personalize mode in
        // practice (the reader only attempts blank-fob personalization while
        // its mode flag is set) — handled as a no-op normal-punch attempt
        // rather than panicking, so a race between the UI toggling mode off
        // and an in-flight reader event fails safe instead of crashing.
        (Mode::Normal, ModeEvent::Tap(TapKind::BlankPersonalized | TapKind::Foreign)) => {
            (Mode::Normal, ModeAction::ProcessNormalPunch)
        }
        (Mode::Normal, ModeEvent::BeendenPressed | ModeEvent::Timeout) => {
            (Mode::Normal, ModeAction::ExitSilently)
        }

        // -- Personalize mode -------------------------------------------------
        (Mode::Personalize { .. }, ModeEvent::Tap(TapKind::Admin)) => {
            (Mode::Normal, ModeAction::ExitPersonalize)
        }
        (Mode::Personalize { .. }, ModeEvent::Tap(TapKind::Employee)) => {
            (Mode::Normal, ModeAction::ExitPersonalizeAndPunch)
        }
        (Mode::Personalize { count }, ModeEvent::Tap(TapKind::BlankPersonalized)) => (
            Mode::Personalize {
                count: count.saturating_add(1),
            },
            ModeAction::IncrementCount,
        ),
        (Mode::Personalize { count }, ModeEvent::Tap(TapKind::Foreign)) => {
            (Mode::Personalize { count }, ModeAction::ShowForeignHint)
        }
        (Mode::Personalize { .. }, ModeEvent::BeendenPressed | ModeEvent::Timeout) => {
            (Mode::Normal, ModeAction::ExitSilently)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // ---- Admin tap toggles mode -----------------------------------------

    #[test]
    fn admin_tap_from_normal_enters_personalize_with_zero_count() {
        let (mode, action) = transition(Mode::Normal, ModeEvent::Tap(TapKind::Admin));
        assert_eq!(mode, Mode::Personalize { count: 0 });
        assert_eq!(action, ModeAction::EnterPersonalize);
    }

    #[test]
    fn admin_tap_from_personalize_exits_to_normal() {
        let (mode, action) = transition(
            Mode::Personalize { count: 7 },
            ModeEvent::Tap(TapKind::Admin),
        );
        assert_eq!(mode, Mode::Normal);
        assert_eq!(action, ModeAction::ExitPersonalize);
    }

    #[test]
    fn admin_tap_toggles_back_and_forth() {
        let (mode, _) = transition(Mode::Normal, ModeEvent::Tap(TapKind::Admin));
        assert!(mode.is_personalize());
        let (mode, _) = transition(mode, ModeEvent::Tap(TapKind::Admin));
        assert_eq!(mode, Mode::Normal);
        let (mode, _) = transition(mode, ModeEvent::Tap(TapKind::Admin));
        assert!(mode.is_personalize());
    }

    // ---- Blank fob -> personalize + count --------------------------------

    #[test]
    fn blank_fob_in_personalize_mode_increments_count() {
        let (mode, action) = transition(
            Mode::Personalize { count: 0 },
            ModeEvent::Tap(TapKind::BlankPersonalized),
        );
        assert_eq!(mode, Mode::Personalize { count: 1 });
        assert_eq!(action, ModeAction::IncrementCount);
    }

    #[test]
    fn blank_fob_batch_of_dozens_increments_sequentially() {
        let mut mode = Mode::Personalize { count: 0 };
        for expected in 1..=42 {
            let (next, action) = transition(mode, ModeEvent::Tap(TapKind::BlankPersonalized));
            assert_eq!(action, ModeAction::IncrementCount);
            assert_eq!(next.count(), expected);
            mode = next;
        }
    }

    #[test]
    fn blank_fob_outside_personalize_mode_is_a_safe_no_op() {
        // Should not occur in practice (reader gates this on its own mode
        // flag), but must not crash or silently enter personalize mode.
        let (mode, action) = transition(Mode::Normal, ModeEvent::Tap(TapKind::BlankPersonalized));
        assert_eq!(mode, Mode::Normal);
        assert_eq!(action, ModeAction::ProcessNormalPunch);
    }

    // ---- Employee tap -> exit + punch -------------------------------------

    #[test]
    fn employee_tap_in_normal_mode_processes_normal_punch() {
        let (mode, action) = transition(Mode::Normal, ModeEvent::Tap(TapKind::Employee));
        assert_eq!(mode, Mode::Normal);
        assert_eq!(action, ModeAction::ProcessNormalPunch);
    }

    #[test]
    fn employee_tap_in_personalize_mode_exits_and_still_punches() {
        let (mode, action) = transition(
            Mode::Personalize { count: 12 },
            ModeEvent::Tap(TapKind::Employee),
        );
        assert_eq!(
            mode,
            Mode::Normal,
            "must never leave an employee stuck in admin mode"
        );
        assert_eq!(action, ModeAction::ExitPersonalizeAndPunch);
    }

    // ---- Foreign card -> hint, no count change -----------------------------

    #[test]
    fn foreign_card_in_personalize_mode_shows_hint_without_changing_count() {
        let (mode, action) = transition(
            Mode::Personalize { count: 5 },
            ModeEvent::Tap(TapKind::Foreign),
        );
        assert_eq!(mode, Mode::Personalize { count: 5 });
        assert_eq!(action, ModeAction::ShowForeignHint);
    }

    #[test]
    fn foreign_card_outside_personalize_mode_is_a_safe_no_op() {
        let (mode, action) = transition(Mode::Normal, ModeEvent::Tap(TapKind::Foreign));
        assert_eq!(mode, Mode::Normal);
        assert_eq!(action, ModeAction::ProcessNormalPunch);
    }

    // ---- Timeout -> exit ---------------------------------------------------

    #[test]
    fn timeout_in_personalize_mode_exits_silently() {
        let (mode, action) = transition(Mode::Personalize { count: 3 }, ModeEvent::Timeout);
        assert_eq!(mode, Mode::Normal);
        assert_eq!(action, ModeAction::ExitSilently);
    }

    #[test]
    fn timeout_in_normal_mode_is_idempotent() {
        let (mode, action) = transition(Mode::Normal, ModeEvent::Timeout);
        assert_eq!(mode, Mode::Normal);
        assert_eq!(action, ModeAction::ExitSilently);
    }

    // ---- Beenden button -> exit ---------------------------------------------

    #[test]
    fn beenden_in_personalize_mode_exits_silently() {
        let (mode, action) = transition(Mode::Personalize { count: 20 }, ModeEvent::BeendenPressed);
        assert_eq!(mode, Mode::Normal);
        assert_eq!(action, ModeAction::ExitSilently);
    }

    #[test]
    fn beenden_in_normal_mode_is_idempotent() {
        let (mode, action) = transition(Mode::Normal, ModeEvent::BeendenPressed);
        assert_eq!(mode, Mode::Normal);
        assert_eq!(action, ModeAction::ExitSilently);
    }

    // ---- Mode helpers -------------------------------------------------------

    #[test]
    fn mode_default_is_normal() {
        assert_eq!(Mode::default(), Mode::Normal);
    }

    #[test]
    fn mode_count_is_zero_outside_personalize() {
        assert_eq!(Mode::Normal.count(), 0);
        assert!(!Mode::Normal.is_personalize());
    }

    #[test]
    fn mode_count_reflects_personalize_tally() {
        let mode = Mode::Personalize { count: 9 };
        assert_eq!(mode.count(), 9);
        assert!(mode.is_personalize());
    }
}
