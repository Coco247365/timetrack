//! Zeiterfassung kiosk terminal.
//!
//! Runs full-screen, listens for NFC card taps on a background thread, and on
//! each tap drives a two-phase flow against the backend:
//!
//! 1. **Preview** (no recording) resolves the employee and their current clock
//!    state.
//! 2. Depending on that state, the kiosk either commits an event immediately
//!    (clock-in / break-end) or shows a touchscreen **choice** (Pause vs
//!    Feierabend). After any committed event a short success screen confirms
//!    the booking.
//!
//! All network calls run on worker threads; every UI mutation is marshalled
//! back onto the Slint event loop via [`slint::invoke_from_event_loop`]. State
//! shared between timers and callbacks lives in single-threaded `Rc<RefCell<…>>`
//! cells, which is sound because those closures all execute on the event-loop
//! thread.

mod api;
mod config;
mod personalize_mode;
mod queue;
mod reader;

use std::cell::RefCell;
use std::rc::Rc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc;
use std::sync::Arc;
use std::thread;
use std::time::Duration;

use anyhow::{Context as _, Result};
use chrono::{Datelike, Local, Months, NaiveDate, SecondsFormat, Utc, Weekday};
use i_slint_backend_winit::WinitWindowAccessor;
use slint::{ComponentHandle, ModelRc, SharedString, Timer, TimerMode, VecModel};

use crate::api::{
    first_name, Action, ApiError, BackendClient, CurrentState, LeaveResult, ProfileResult,
    PunchResult, RegisterToPoolResult,
};
use crate::config::{Config, ReaderKind};
use crate::personalize_mode::{Mode as PersonalizeMode, ModeAction, ModeEvent, TapKind};
use crate::queue::{new_client_event_id, OfflineQueue, QueuedPunch};
use crate::reader::{mock::MockReader, CardEvent, CardReader};

// Generated from `ui/terminal.slint` by `build.rs`.
slint::include_modules!();

/// How long a committed-event success screen stays up before returning to
/// idle.
const SUCCESS_WINDOW: Duration = Duration::from_secs(3);
/// How long an error message stays up.
const NOTICE_WINDOW: Duration = Duration::from_secs(2);
/// How long the Unpaired-Fob screen (chip UID for an unassigned tap) stays
/// up — longer than [`NOTICE_WINDOW`] so an operator has time to read (or
/// write down) the UID before it walks away back to idle.
const UNPAIRED_WINDOW: Duration = Duration::from_secs(10);
/// How often the offline queue is retried against the backend.
const QUEUE_FLUSH_INTERVAL: Duration = Duration::from_secs(30);
/// Wie oft die Rundmeldungen fuer den Ruhebildschirm abgeholt werden (#189).
const BROADCAST_POLL_INTERVAL: Duration = Duration::from_secs(120);
/// How long the menu waits for a button before walking away.
const MENU_TIMEOUT: Duration = Duration::from_secs(15);
/// How long the profile screen stays up before returning to idle.
const PROFILE_WINDOW: Duration = Duration::from_secs(20);
/// How long the vacation calendar waits between interactions before it walks
/// away (re-armed on every tap).
const LEAVE_TIMEOUT: Duration = Duration::from_secs(45);
/// Terminal personalize mode (admin-fob-gated batch fob personalization):
/// how long the screen waits for a tap before walking away (re-armed on
/// every tap while the mode is active — see `arm_personalize_timeout`).
const PERSONALIZE_TIMEOUT: Duration = Duration::from_secs(5 * 60);
/// How long a per-fob hint ("Fob personalisiert" / "Kein leerer Chip
/// erkannt") stays on the personalize screen before clearing (the screen
/// itself stays up — this only clears the transient `detail` line).
const PERSONALIZE_HINT_WINDOW: Duration = Duration::from_secs(3);

/// Which set of action buttons the menu offers, derived from the employee's
/// current clock state.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum MenuMode {
    /// Clocked out → [Arbeitszeit starten].
    Out,
    /// Clocked in → [Pause] [Arbeitszeit beenden].
    In,
    /// On break → [Pause beenden].
    Break,
}

impl MenuMode {
    /// The string the Slint `menu-mode` property expects.
    fn as_slint(self) -> &'static str {
        match self {
            Self::Out => "out",
            Self::In => "in",
            Self::Break => "break",
        }
    }

    /// Map a previewed clock state onto a menu, or `None` for an unknown state.
    fn from_state(state: &CurrentState) -> Option<Self> {
        match state {
            CurrentState::ClockedOut => Some(Self::Out),
            CurrentState::ClockedIn => Some(Self::In),
            CurrentState::OnBreak => Some(Self::Break),
            CurrentState::Other(_) => None,
        }
    }
}

/// Shared per-flow state, mutated only on the event-loop thread.
///
/// `pending_badge` is the badge currently eligible for a menu or profile
/// action. `menu_mode` and `first_name` are retained while a menu/profile is on
/// screen so the profile screen's "Zurück" button can rebuild the right menu.
/// All three are cleared whenever we return to idle so a stale button press
/// cannot fire against an old badge.
#[derive(Default)]
struct UiState {
    pending_badge: Option<String>,
    menu_mode: Option<MenuMode>,
    first_name: Option<String>,
    /// Selected start/end of a vacation request while the calendar is up.
    leave_from: Option<NaiveDate>,
    leave_to: Option<NaiveDate>,
    /// First day of the month currently shown in the calendar.
    cal_month: Option<NaiveDate>,
}

/// Bundle of the handles the event-loop closures need, cloned cheaply into each
/// callback.
struct App {
    weak: slint::Weak<MainWindow>,
    client: Arc<BackendClient>,
    /// Offline-Warteschlange: nimmt Stempel auf, wenn das Backend nicht
    /// erreichbar ist; der Flush-Thread reicht sie nach.
    queue: Arc<OfflineQueue>,
    state: Rc<RefCell<UiState>>,
    /// Returns the UI to idle after a result/notice is shown.
    reset_timer: Rc<Timer>,
    /// Walks away from an unanswered choice screen.
    choice_timer: Rc<Timer>,
    /// Terminal personalize mode (admin-fob-gated batch fob personalization):
    /// current mode, mutated only on the event-loop thread via
    /// [`personalize_mode::transition`]. See the module doc there for the
    /// full state machine.
    mode: Rc<RefCell<PersonalizeMode>>,
    /// Mirrors `mode.borrow().is_personalize()` for the reader thread (which
    /// cannot borrow `Rc<RefCell<_>>` across threads) — see
    /// [`sync_personalize_flag`]. Read by `reader::pcsc_reader`/`reader::mock`
    /// to decide whether to attempt blank-fob personalization on an auth
    /// failure.
    personalize_flag: Arc<AtomicBool>,
    /// Walks away from an idle personalize screen after 5 minutes of
    /// inactivity (re-armed on every personalize-mode tap).
    personalize_timeout_timer: Rc<Timer>,
    /// Clears the personalize screen's transient per-fob hint text.
    personalize_hint_timer: Rc<Timer>,
}

impl App {
    fn upgrade(&self) -> Option<MainWindow> {
        self.weak.upgrade()
    }
}

fn main() -> Result<()> {
    let config = Config::from_env().context(
        "failed to load kiosk configuration from environment \
         (is KIOSK_TENANT_ID set to a tenant UUID?)",
    )?;

    let client = BackendClient::new(&config)
        .map_err(|e| anyhow::anyhow!("failed to build HTTP client: {e}"))?;

    let window = MainWindow::new().context("failed to create kiosk window")?;

    // Channel: reader thread -> UI thread.
    let (card_tx, card_rx) = mpsc::channel::<CardEvent>();

    // Terminal personalize mode (admin-fob-gated batch fob personalization):
    // shared with the reader thread so it knows whether to attempt blank-fob
    // personalization on an auth failure — see `personalize_mode` and
    // `sync_personalize_flag`. Built before the reader so it can be moved in.
    let personalize_flag = Arc::new(AtomicBool::new(false));

    // Spawn the selected reader on its own thread.
    let card_reader: Box<dyn CardReader> = build_reader(&config, Arc::clone(&personalize_flag))?;
    thread::Builder::new()
        .name("card-reader".to_owned())
        .spawn(move || card_reader.run(card_tx))
        .context("failed to spawn reader thread")?;

    let offline_queue = Arc::new(OfflineQueue::from_env());

    let app = Rc::new(App {
        weak: window.as_weak(),
        client: Arc::new(client),
        queue: Arc::clone(&offline_queue),
        state: Rc::new(RefCell::new(UiState::default())),
        reset_timer: Rc::new(Timer::default()),
        choice_timer: Rc::new(Timer::default()),
        mode: Rc::new(RefCell::new(PersonalizeMode::default())),
        personalize_flag,
        personalize_timeout_timer: Rc::new(Timer::default()),
        personalize_hint_timer: Rc::new(Timer::default()),
    });

    // Stash the App on the event-loop thread so worker continuations (which can
    // only carry a `Weak<MainWindow>`) can recover it via `app_from_window`.
    APP.with(|cell| *cell.borrow_mut() = Some(Rc::clone(&app)));

    // Wire the touchscreen callbacks.
    {
        let app_start = Rc::clone(&app);
        window.on_action_start(move || on_menu_action(&app_start, Action::PunchIn));

        let app_pause = Rc::clone(&app);
        window.on_action_pause(move || on_menu_action(&app_pause, Action::BreakStart));

        let app_feier = Rc::clone(&app);
        window.on_action_feierabend(move || on_menu_action(&app_feier, Action::PunchOut));

        let app_resume = Rc::clone(&app);
        window.on_action_resume(move || on_menu_action(&app_resume, Action::BreakEnd));

        let app_profile = Rc::clone(&app);
        window.on_show_profile(move || on_profile(&app_profile));

        let app_cancel = Rc::clone(&app);
        window.on_cancel(move || on_cancel(&app_cancel));

        let app_back = Rc::clone(&app);
        window.on_profile_back(move || on_profile_back(&app_back));

        // Vacation calendar.
        let app_leave = Rc::clone(&app);
        window.on_show_leave(move || on_show_leave(&app_leave));

        let app_cp = Rc::clone(&app);
        window.on_cal_prev(move || cal_shift(&app_cp, -1));
        let app_cn = Rc::clone(&app);
        window.on_cal_next(move || cal_shift(&app_cn, 1));
        let app_cd = Rc::clone(&app);
        window.on_cal_day(move |day| on_cal_day(&app_cd, day));

        let app_ls = Rc::clone(&app);
        window.on_leave_submit(move || on_leave_submit(&app_ls));

        // Terminal personalize mode: "Beenden" button.
        let app_pexit = Rc::clone(&app);
        window.on_personalize_exit(move || {
            eprintln!("[personalize] Beenden-Button clicked callback fired");
            exit_personalize_mode(&app_pexit, ModeEvent::BeendenPressed);
        });
    }

    // Flush-Thread: reicht wartende Offline-Stempel nach, sobald das Backend
    // wieder erreichbar ist, und haelt die Idle-Statuszeile aktuell.
    {
        let client = Arc::clone(&app.client);
        let queue = Arc::clone(&offline_queue);
        let weak = window.as_weak();
        thread::Builder::new()
            .name("queue-flush".to_owned())
            .spawn(move || loop {
                let pending = flush_queue(&client, &queue);
                update_queue_info(&weak, pending);
                thread::sleep(QUEUE_FLUSH_INTERVAL);
            })
            .context("failed to spawn queue-flush thread")?;
    }

    // Rundmeldungen-Poll: holt periodisch die aktiven Kiosk-Rundmeldungen und
    // zeigt sie auf dem Ruhebildschirm (#189). Fehler werden ignoriert
    // (Backend kurz weg -> beim naechsten Durchlauf erneut).
    {
        let client = Arc::clone(&app.client);
        let weak = window.as_weak();
        thread::Builder::new()
            .name("broadcast-poll".to_owned())
            .spawn(move || loop {
                if let Ok(msgs) = client.kiosk_messages() {
                    let text = msgs.join("\n");
                    let weak = weak.clone();
                    let _ = slint::invoke_from_event_loop(move || {
                        if let Some(w) = weak.upgrade() {
                            w.set_broadcast(SharedString::from(text));
                        }
                    });
                }
                thread::sleep(BROADCAST_POLL_INTERVAL);
            })
            .context("failed to spawn broadcast-poll thread")?;
    }

    // Drain reader events on the UI thread via a polling timer (keeps all UI
    // mutation on the event loop thread, which Slint requires).
    let poll_timer = Timer::default();
    {
        let app = Rc::clone(&app);
        poll_timer.start(TimerMode::Repeated, Duration::from_millis(50), move || {
            while let Ok(event) = card_rx.try_recv() {
                match event {
                    CardEvent::Detected { token, uid } => on_card_detected(&app, token, uid),
                    // No action on removal; the timed reset returns to idle.
                    CardEvent::Removed => {}
                    // Terminal personalize mode (only ever emitted while the
                    // reader's mirrored `personalize_flag` is set — see
                    // `sync_personalize_flag`).
                    CardEvent::Personalized { token, uid } => {
                        on_blank_personalized(&app, token, uid);
                    }
                    CardEvent::PersonalizeRejected => on_personalize_rejected(&app),
                }
            }
        });
    }

    // `window.run()` is normally just `show()` + `run_event_loop()` + `hide()`
    // (the generated component's convenience wrapper). We split it out here
    // to hide the mouse cursor app-side right after `show()` — the kiosk is a
    // touch-only wall terminal with no pointer device, but labwc/Wayland can
    // still render a cursor (e.g. a stray USB mouse, or the compositor's
    // default). `with_winit_window` needs the native winit window to already
    // exist; that normally happens synchronously inside `show()`, but if the
    // backend hasn't created it yet (timing depends on the compositor), fall
    // back to a zero-delay single-shot timer that retries on the first
    // event-loop tick.
    window.show().context("failed to show kiosk window")?;
    if window
        .window()
        .with_winit_window(|w| w.set_cursor_visible(false))
        .is_none()
    {
        eprintln!(
            "[cursor] winit window not ready right after show(); retrying on first event-loop tick"
        );
        let weak = window.as_weak();
        Timer::single_shot(Duration::from_millis(0), move || {
            let Some(window) = weak.upgrade() else {
                return;
            };
            let hidden = window
                .window()
                .with_winit_window(|w| w.set_cursor_visible(false))
                .is_some();
            if !hidden {
                eprintln!(
                    "[cursor] winit window still unavailable on first tick; cursor may stay visible"
                );
            }
        });
    }

    slint::run_event_loop().context("Slint event loop terminated with an error")?;
    window.hide().ok();
    Ok(())
}

/// Construct the configured reader. Returns an error if the real reader is
/// requested but was not compiled in.
///
/// `personalize_flag` is handed to the reader so it knows — purely as a
/// runtime toggle, mirrored from `App.mode` by [`sync_personalize_flag`] —
/// whether to attempt blank-fob personalization on an auth failure (terminal
/// personalize mode). The mock reader also takes it (see `reader::mock`) so
/// the feature is testable locally with no hardware.
fn build_reader(config: &Config, personalize_flag: Arc<AtomicBool>) -> Result<Box<dyn CardReader>> {
    match config.reader {
        ReaderKind::Mock => Ok(Box::new(MockReader::new(personalize_flag))),
        ReaderKind::Pcsc => {
            #[cfg(feature = "real-reader")]
            {
                use crate::reader::sam::personalize::{default_personalize_config, FobKeys};
                use crate::reader::sam::punch::PunchAuthConfig;

                // Blank-fob personalization reuses the same AID/company-key
                // configuration the punch path already requires at startup
                // (sam_aid, sam_key_entry_no/_ver) — the 2026-07-15 offline-
                // personalization rework authenticates a blank fob's
                // factory-default key entirely in software, so no
                // *additional* SAM key entry is needed here; personalize
                // mode is always usable once the terminal has a working
                // PC/SC/SAM setup at all (same convention
                // `sam_personalize`'s `--company-key-entry-no` uses).
                let personalize_cfg = default_personalize_config(
                    config.sam_aid,
                    config.sam_app_key_no,
                    config.sam_file_no,
                    FobKeys {
                        company_key_entry_no: config.sam_key_entry_no,
                        company_key_entry_ver: config.sam_key_entry_ver,
                    },
                );

                Ok(Box::new(crate::reader::pcsc_reader::PcscReader::new(
                    config.sam_reader_substr.clone(),
                    config.picc_reader_substr.clone(),
                    PunchAuthConfig {
                        aid: config.sam_aid,
                        app_key_no: config.sam_app_key_no,
                        sam_key_entry_no: config.sam_key_entry_no,
                        sam_key_ver: config.sam_key_entry_ver,
                        file_no: config.sam_file_no,
                        token_id_len: config.sam_token_id_len,
                    },
                    personalize_flag,
                    personalize_cfg,
                )))
            }
            #[cfg(not(feature = "real-reader"))]
            {
                let _ = personalize_flag;
                anyhow::bail!("KIOSK_READER=pcsc requires building with --features real-reader")
            }
        }
    }
}

/// Phase 1: a card was tapped — preview it on a worker thread, then branch.
///
/// A `Detected` tap is always previewed (it authenticated as *some* company
/// fob — employee or admin) so [`dispatch_preview`] can read `is_admin` and
/// feed the terminal personalize-mode state machine; see
/// [`personalize_mode`]. `uid` (the card's chip UID, hex) is carried through
/// to [`dispatch_preview`] purely so an unknown/unassigned badge (HTTP 404)
/// can be backfilled into the backend's scan pool with its UID (bead rst) —
/// it plays no role in authentication, which already happened in the reader.
fn on_card_detected(app: &Rc<App>, badge: String, uid: String) {
    // A fresh tap supersedes any pending menu/profile/personalize
    // walk-away.
    app.choice_timer.stop();
    app.reset_timer.stop();
    app.personalize_timeout_timer.stop();
    app.personalize_hint_timer.stop();
    clear_pending(app);

    set_busy(app);

    let client = Arc::clone(&app.client);
    let weak = app.weak.clone();
    spawn_worker("preview-worker", move || {
        let result = client.preview(&badge);
        let _ = slint::invoke_from_event_loop(move || {
            // Re-acquire the App from the surviving weak handle's user data is
            // not possible; instead we recompute on the captured `weak`. We
            // pass the work back through a thin re-dispatch below.
            dispatch_preview(weak, badge, uid, result);
        });
    });
}

/// Continuation of [`on_card_detected`] on the event-loop thread.
///
/// The worker can only carry `Send` data, so it captures a `Weak<MainWindow>`
/// plus the badge/uid; the rest of the [`App`] (shared state, timers) is
/// recovered from the startup-stashed thread-local via [`app_from_window`].
fn dispatch_preview(
    weak: slint::Weak<MainWindow>,
    badge: String,
    uid: String,
    result: Result<api::PreviewResult, ApiError>,
) {
    let Some(window) = weak.upgrade() else {
        return;
    };
    // Recover the App from the window's stashed pointer.
    let Some(app) = app_from_window(&window) else {
        return;
    };

    match result {
        // Backend nicht erreichbar: Tap lokal puffern (Aktion unbekannt ->
        // wird beim Nachreichen ueber /nfc/auto aus dem Serverzustand
        // abgeleitet) und dem Mitarbeiter "gespeichert" zeigen.
        Err(ApiError::Transport(_)) => {
            let punch = QueuedPunch {
                client_event_id: new_client_event_id(),
                badge_id: badge.clone(),
                event_type: None,
                event_time: Utc::now().to_rfc3339_opts(SecondsFormat::Secs, true),
            };
            app.queue.append(&punch);
            show_offline_saved(&app);
        }
        Err(err) => {
            // Unknown/unassigned badge (HTTP 404): this token authenticated
            // fine (the reader only emits `Detected` after a successful
            // SAM/EV2 auth) but isn't resolved to an employee yet — never an
            // admin fob (those resolve `Ok` with `is_admin=true` below).
            // Silently backfill its chip UID into the backend's scan pool
            // (bead rst) so the web-app can later match it by phone-scanned
            // UID; failures here are logged only, never shown to the
            // employee. Instead of the generic "Fehler" notice, show the
            // dedicated Unpaired-Fob screen (bead 2hv) with the chip UID so
            // an operator standing at the terminal can read it off directly
            // and assign it in the web app — never a token/key. Every other
            // `ApiError` variant (transport already handled above, 409,
            // 429, other HTTP) still goes through the generic error screen.
            if matches!(err, ApiError::UnknownBadge(_)) {
                let client = Arc::clone(&app.client);
                let token = badge.clone();
                let uid_for_backfill = uid.clone();
                spawn_worker("register-backfill-worker", move || {
                    if let Err(e) = client.register_to_pool(&[token], &[Some(uid_for_backfill)]) {
                        eprintln!("[api] register-to-pool Backfill fehlgeschlagen: {e}");
                    }
                });
                show_unpaired(&app, &uid);
            } else {
                show_error(&app, &err);
            }
        }
        Ok(preview) => {
            // Terminal personalize mode (admin-fob-gated batch fob
            // personalization): feed the pure state machine with the tap
            // kind derived from `is_admin`. Every admin tap toggles the
            // mode (even a badge that ALSO resolves to an employee — the
            // admin action takes priority over punching, see
            // `personalize_mode`'s module doc); a plain employee tap either
            // processes normally (mode already off) or exits mode AND still
            // punches (mode was on).
            let kind = if preview.is_admin {
                TapKind::Admin
            } else {
                TapKind::Employee
            };
            let action = apply_mode_event(&app, ModeEvent::Tap(kind));

            match action {
                ModeAction::EnterPersonalize => enter_personalize_screen(&app),
                ModeAction::ExitPersonalize => {
                    app.personalize_timeout_timer.stop();
                    app.personalize_hint_timer.stop();
                    clear_pending(&app);
                    if let Some(window) = app.upgrade() {
                        reset_to_idle(&window);
                    }
                }
                ModeAction::ProcessNormalPunch | ModeAction::ExitPersonalizeAndPunch => {
                    if action == ModeAction::ExitPersonalizeAndPunch {
                        app.personalize_timeout_timer.stop();
                        app.personalize_hint_timer.stop();
                    }
                    let first = first_name(&preview.employee_name).to_owned();
                    match MenuMode::from_state(&preview.current_state) {
                        // Every recognised state now opens a menu rather than
                        // committing automatically: the employee always gets
                        // to choose (and can view their profile or cancel).
                        Some(menu_mode) => show_menu(&app, badge, menu_mode, &first),
                        None => show_error(
                            &app,
                            &ApiError::InvalidTransition(
                                "Unbekannter Status — bitte erneut versuchen".to_owned(),
                            ),
                        ),
                    }
                }
                // A Tap(Admin)/Tap(Employee) event never yields these three
                // (see `personalize_mode::transition`'s exhaustive match) —
                // unreachable in practice, handled defensively rather than
                // panicking.
                ModeAction::IncrementCount
                | ModeAction::ShowForeignHint
                | ModeAction::ExitSilently => {}
            }
        }
    }
}

/// Apply one [`ModeEvent`] to `app.mode`, sync the reader-thread mirror flag,
/// and return the resulting [`ModeAction`]. The single choke point every
/// personalize-mode transition in `main.rs` goes through.
fn apply_mode_event(app: &Rc<App>, event: ModeEvent) -> ModeAction {
    let (new_mode, action) = {
        let mut mode = app.mode.borrow_mut();
        let (m, a) = crate::personalize_mode::transition(*mode, event);
        *mode = m;
        (m, a)
    };
    sync_personalize_flag(app, new_mode);
    action
}

/// Mirror `app.mode`'s "is personalize mode active" bit onto the
/// `Arc<AtomicBool>` shared with the reader thread (see [`build_reader`]).
fn sync_personalize_flag(app: &Rc<App>, mode: PersonalizeMode) {
    app.personalize_flag
        .store(mode.is_personalize(), Ordering::Relaxed);
}

/// Enter personalize mode: render the screen and arm the 5-minute
/// inactivity walk-away.
fn enter_personalize_screen(app: &Rc<App>) {
    clear_pending(app);
    render_personalize_screen(app);
    arm_personalize_timeout(app);
}

/// (Re-)render the personalize screen from the current `app.mode` count.
/// Used both when entering the mode and when returning to it after a
/// transient screen (e.g. an unrelated badge's error notice) finishes — see
/// [`arm_reset`]'s mode-aware branch.
fn render_personalize_screen(app: &Rc<App>) {
    let count = app.mode.borrow().count();
    if let Some(window) = app.upgrade() {
        window.set_state(SharedString::from("personalize"));
        window.set_heading(SharedString::from("Personalisierung aktiv"));
        window.set_message(SharedString::from(format!(
            "{count} Fob{} personalisiert",
            if count == 1 { "" } else { "s" }
        )));
        window.set_detail(SharedString::from(""));
    }
}

/// (Re-)arm the personalize screen's 5-minute inactivity walk-away.
fn arm_personalize_timeout(app: &Rc<App>) {
    let app_t = Rc::clone(app);
    app.personalize_timeout_timer
        .start(TimerMode::SingleShot, PERSONALIZE_TIMEOUT, move || {
            exit_personalize_mode(&app_t, ModeEvent::Timeout);
        });
}

/// Exit personalize mode (Beenden button or 5-minute inactivity timeout) and
/// return to idle. A no-op (idempotent) if mode is already `Normal` — see
/// `personalize_mode::transition`.
fn exit_personalize_mode(app: &Rc<App>, event: ModeEvent) {
    eprintln!("[personalize] exit_personalize_mode event={event:?}");
    apply_mode_event(app, event);
    app.personalize_timeout_timer.stop();
    app.personalize_hint_timer.stop();
    clear_pending(app);
    if let Some(window) = app.upgrade() {
        reset_to_idle(&window);
    }
}

/// A blank fob was successfully personalized by the reader (terminal
/// personalize mode, only ever emitted while `personalize_flag` was set —
/// see `CardEvent::Personalized`'s doc comment). Register it — token-id AND
/// chip UID, bead rst — with the backend scan pool before counting it as
/// done.
fn on_blank_personalized(app: &Rc<App>, token_id_hex: String, uid_hex: String) {
    // A personalize-related tap is activity — keep the 5-minute inactivity
    // timer from firing mid-batch.
    if app.mode.borrow().is_personalize() {
        arm_personalize_timeout(app);
    }

    let client = Arc::clone(&app.client);
    let weak = app.weak.clone();
    spawn_worker("personalize-register-worker", move || {
        let uid_opt = Some(uid_hex);
        let result = client.register_to_pool(
            std::slice::from_ref(&token_id_hex),
            std::slice::from_ref(&uid_opt),
        );
        let _ = slint::invoke_from_event_loop(move || {
            let Some(window) = weak.upgrade() else { return };
            let Some(app) = app_from_window(&window) else {
                return;
            };
            dispatch_blank_personalized(&app, result);
        });
    });
}

/// Continuation of [`on_blank_personalized`] on the event-loop thread.
fn dispatch_blank_personalized(app: &Rc<App>, result: Result<RegisterToPoolResult, ApiError>) {
    match result {
        Ok(_reg) => {
            let action = apply_mode_event(app, ModeEvent::Tap(TapKind::BlankPersonalized));
            if action == ModeAction::IncrementCount {
                render_personalize_screen(app);
                show_personalize_hint(app, "Fob personalisiert");
            }
            // Any other action (the safe ProcessNormalPunch fallback for a
            // stray event arriving just after mode was exited) is a no-op:
            // the fob is still pooled in the backend either way, just not
            // reflected in this session's on-screen count.
        }
        Err(err) => {
            // The fob is physically personalized but NOT yet in the backend
            // pool — the count is intentionally not incremented. Operator
            // should retry once connectivity is back (retap, or finish this
            // fob via the `sam_personalize` SSH CLI); the terminal does not
            // persist not-yet-registered token-ids across a restart.
            if app.mode.borrow().is_personalize() {
                show_personalize_hint(app, &format!("Registrierung fehlgeschlagen: {err}"));
            }
        }
    }
}

/// A tap failed both normal auth AND blank-fob personalization (foreign/
/// non-DESFire/damaged card) — only ever emitted while `personalize_flag`
/// was set.
fn on_personalize_rejected(app: &Rc<App>) {
    if app.mode.borrow().is_personalize() {
        arm_personalize_timeout(app);
    }
    let action = apply_mode_event(app, ModeEvent::Tap(TapKind::Foreign));
    if action == ModeAction::ShowForeignHint {
        show_personalize_hint(app, "Kein leerer Chip erkannt");
    }
}

/// Show a transient hint on the personalize screen's `detail` line, clearing
/// it after [`PERSONALIZE_HINT_WINDOW`] — the screen itself (and the count)
/// stays up throughout.
fn show_personalize_hint(app: &Rc<App>, text: &str) {
    if let Some(window) = app.upgrade() {
        window.set_detail(SharedString::from(text.to_owned()));
    }
    let app_t = Rc::clone(app);
    app.personalize_hint_timer
        .start(TimerMode::SingleShot, PERSONALIZE_HINT_WINDOW, move || {
            if app_t.mode.borrow().is_personalize() {
                if let Some(window) = app_t.upgrade() {
                    window.set_detail(SharedString::from(""));
                }
            }
        });
}

/// Clear the per-flow pending state so no stale button press can act on an old
/// badge once we leave a menu/profile/success screen.
fn clear_pending(app: &Rc<App>) {
    let mut st = app.state.borrow_mut();
    st.pending_badge = None;
    st.menu_mode = None;
    st.first_name = None;
    st.leave_from = None;
    st.leave_to = None;
    st.cal_month = None;
}

/// Show the action menu for the resolved badge and arm the walk-away timeout.
///
/// The menu always offers "Profil anschauen" and "Abbrechen" alongside the
/// state-specific action(s); the chosen action is only recorded when a button
/// is pressed (nothing is committed automatically).
fn show_menu(app: &Rc<App>, badge: String, mode: MenuMode, first: &str) {
    {
        let mut st = app.state.borrow_mut();
        st.pending_badge = Some(badge);
        st.menu_mode = Some(mode);
        st.first_name = Some(first.to_owned());
    }

    if let Some(window) = app.upgrade() {
        window.set_state(SharedString::from("menu"));
        window.set_menu_mode(SharedString::from(mode.as_slint()));
        window.set_heading(SharedString::from(format!("Hallo, {first}!")));
        window.set_message(SharedString::from(""));
        window.set_detail(SharedString::from(""));
    }

    // No button within the window → walk away, recording nothing.
    let app_t = Rc::clone(app);
    app.choice_timer.start(TimerMode::SingleShot, MENU_TIMEOUT, move || {
        clear_pending(&app_t);
        if let Some(window) = app_t.upgrade() {
            reset_to_idle(&window);
        }
    });
}

/// A menu action button was pressed: commit the chosen action for the pending
/// badge.
fn on_menu_action(app: &Rc<App>, action: Action) {
    app.choice_timer.stop();
    let badge = app.state.borrow().pending_badge.clone();
    let Some(badge) = badge else {
        // No pending badge (timed out or already handled) — ignore.
        return;
    };
    commit(app, &badge, action);
}

/// "Abbrechen" was pressed (or chosen from the menu): drop everything and return
/// to idle.
///
/// Also doubles as the personalize screen's ✕-exit (see `terminal.slint`'s red
/// ✕ condition) — a proven-working touch target added alongside "Beenden"
/// after that dedicated button failed to react on real hardware. When the app
/// is in personalize mode this must go through `exit_personalize_mode` (not
/// just `reset_to_idle`) so the mode/flag/timers are torn down cleanly.
fn on_cancel(app: &Rc<App>) {
    if app.mode.borrow().is_personalize() {
        exit_personalize_mode(app, ModeEvent::BeendenPressed);
        return;
    }
    app.choice_timer.stop();
    app.reset_timer.stop();
    clear_pending(app);
    if let Some(window) = app.upgrade() {
        reset_to_idle(&window);
    }
}

/// "Profil anschauen" was pressed: fetch the employee's live profile on a worker
/// thread, keeping the pending badge/menu so "Zurück" can rebuild the menu.
fn on_profile(app: &Rc<App>) {
    app.choice_timer.stop();
    app.reset_timer.stop();
    let badge = app.state.borrow().pending_badge.clone();
    let Some(badge) = badge else {
        return;
    };

    set_busy(app);

    let client = Arc::clone(&app.client);
    let weak = app.weak.clone();
    spawn_worker("profile-worker", move || {
        let result = client.profile(&badge);
        let _ = slint::invoke_from_event_loop(move || {
            dispatch_profile(weak, result);
        });
    });
}

/// Continuation of [`on_profile`] on the event-loop thread.
fn dispatch_profile(weak: slint::Weak<MainWindow>, result: Result<ProfileResult, ApiError>) {
    let Some(window) = weak.upgrade() else {
        return;
    };
    let Some(app) = app_from_window(&window) else {
        return;
    };

    match result {
        Ok(profile) => show_profile(&app, &profile),
        Err(err) => show_error(&app, &err),
    }
}

/// "HH:MM Uhr" in Lokalzeit aus einem optionalen ISO-Zeitstempel, sonst ein
/// Gedankenstrich (z.B. "Ausgestempelt: —" solange die Schicht laeuft).
fn fmt_time_opt(iso: Option<&str>) -> String {
    use chrono::DateTime;
    let Some(raw) = iso.filter(|s| !s.is_empty()) else {
        return "—".to_owned();
    };
    if let Ok(dt) = DateTime::parse_from_rfc3339(raw) {
        return format!("{} Uhr", dt.with_timezone(&Local).format("%H:%M"));
    }
    // Tolerante Fallback-Extraktion wie in format_event_time.
    if let Some((_, rest)) = raw.split_once('T') {
        let time = rest.split(['.', '+', '-', 'Z']).next().unwrap_or(rest);
        let mut parts = time.split(':');
        if let (Some(h), Some(m)) = (parts.next(), parts.next()) {
            if h.len() == 2 && m.len() == 2 {
                return format!("{h}:{m} Uhr");
            }
        }
    }
    "—".to_owned()
}

/// Render the profile screen and arm an auto-return-to-idle timeout. The pending
/// badge/menu are kept so the "Zurück" button can rebuild the menu.
fn show_profile(app: &Rc<App>, profile: &ProfileResult) {
    if let Some(window) = app.upgrade() {
        window.set_state(SharedString::from("profile"));
        window.set_heading(SharedString::from(display_name(&profile.employee_name)));
        window.set_message(SharedString::from(""));
        window.set_detail(SharedString::from(""));
        window.set_prof_status(SharedString::from(profile.current_state.label_de()));
        window.set_prof_in(SharedString::from(fmt_time_opt(profile.today_start.as_deref())));
        window.set_prof_out(SharedString::from(fmt_time_opt(profile.today_end.as_deref())));
        window.set_prof_today(SharedString::from(fmt_hm(profile.today_work_minutes)));
        window.set_prof_break(SharedString::from(fmt_hm(profile.today_break_minutes)));
        window.set_prof_overtime(SharedString::from(fmt_hm_signed(profile.today_overtime_minutes)));
        window.set_prof_konto(SharedString::from(fmt_hm_signed(profile.konto_balance_minutes)));
        window.set_prof_vacation(SharedString::from(fmt_days_opt(profile.vacation_remaining_days)));
        window.set_prof_contract(SharedString::from(fmt_contract(
            profile.contracted_hours_per_week.as_deref(),
        )));
    }

    // Auto-return to idle after the profile window; "Zurück" interrupts sooner.
    let app_t = Rc::clone(app);
    app.reset_timer.start(TimerMode::SingleShot, PROFILE_WINDOW, move || {
        clear_pending(&app_t);
        if let Some(window) = app_t.upgrade() {
            reset_to_idle(&window);
        }
    });
}

/// "Zurück" on the profile screen: rebuild the menu for the still-pending badge.
fn on_profile_back(app: &Rc<App>) {
    app.reset_timer.stop();
    let (badge, mode, first) = {
        let st = app.state.borrow();
        (st.pending_badge.clone(), st.menu_mode, st.first_name.clone())
    };
    match (badge, mode, first) {
        (Some(badge), Some(mode), Some(first)) => show_menu(app, badge, mode, &first),
        _ => {
            clear_pending(app);
            if let Some(window) = app.upgrade() {
                reset_to_idle(&window);
            }
        }
    }
}

/// "Urlaub beantragen" was pressed: open the month calendar with no range yet,
/// keeping the pending badge so the ✕ can cancel.
fn on_show_leave(app: &Rc<App>) {
    app.choice_timer.stop();
    app.reset_timer.stop();
    if app.state.borrow().pending_badge.is_none() {
        return;
    }
    {
        let mut st = app.state.borrow_mut();
        st.leave_from = None;
        st.leave_to = None;
        st.cal_month = Some(first_of_month(today()));
    }
    if let Some(window) = app.upgrade() {
        window.set_state(SharedString::from("leave"));
        window.set_heading(SharedString::from("Urlaub beantragen"));
        window.set_message(SharedString::from(""));
        window.set_detail(SharedString::from(""));
    }
    render_leave(app);
    arm_leave_timeout(app);
}

/// Rebuild the calendar model + range summary from the current state.
fn render_leave(app: &Rc<App>) {
    let (month, from, to) = {
        let st = app.state.borrow();
        (st.cal_month, st.leave_from, st.leave_to)
    };
    let Some(month) = month else {
        return;
    };
    let cells = build_calendar(month, from, to, today());
    let summary = leave_summary(from, to);
    if let Some(window) = app.upgrade() {
        window.set_cal_month(SharedString::from(month_label_de(month)));
        window.set_cal_days(ModelRc::new(VecModel::from(cells)));
        window.set_leave_days(SharedString::from(summary));
    }
}

/// Build the 6×7 day-cell grid for `month`, marking the selected range and
/// disabling days before `today`.
fn build_calendar(
    month: NaiveDate,
    from: Option<NaiveDate>,
    to: Option<NaiveDate>,
    today: NaiveDate,
) -> Vec<DayCell> {
    let lead = month.weekday().num_days_from_monday() as i32; // 0 = Monday
    let days_in_month = days_in_month(month);
    let mut cells = Vec::with_capacity(42);
    for p in 0..42 {
        let col = p % 7;
        let row = p / 7;
        let day_num = p - lead + 1; // 1-based day of month at this slot
        if day_num < 1 || day_num > days_in_month {
            cells.push(DayCell {
                label: SharedString::new(),
                day_index: 0,
                col,
                row,
                in_range: false,
                is_edge: false,
                selectable: false,
            });
            continue;
        }
        let date = month.with_day(day_num as u32).unwrap();
        let (in_range, is_edge) = match (from, to) {
            (Some(f), Some(t)) => (date >= f && date <= t, date == f || date == t),
            (Some(f), None) => (false, date == f),
            _ => (false, false),
        };
        cells.push(DayCell {
            label: SharedString::from(format!("{day_num}")),
            day_index: day_num,
            col,
            row,
            in_range,
            is_edge,
            selectable: date >= today,
        });
    }
    cells
}

/// Human range summary shown under the calendar.
fn leave_summary(from: Option<NaiveDate>, to: Option<NaiveDate>) -> String {
    match (from, to) {
        (Some(f), Some(t)) => {
            let days = (t - f).num_days() + 1;
            let unit = if days == 1 { "Tag" } else { "Tage" };
            format!("{} – {}  ·  {days} {unit}", fmt_de_date(f), fmt_de_date(t))
        }
        (Some(f), None) => format!("Ab {} – Enddatum wählen", fmt_de_date(f)),
        _ => "Startdatum wählen".to_owned(),
    }
}

/// Re-arm the calendar walk-away timer (on entry and each tap).
fn arm_leave_timeout(app: &Rc<App>) {
    let app_t = Rc::clone(app);
    app.choice_timer.start(TimerMode::SingleShot, LEAVE_TIMEOUT, move || {
        clear_pending(&app_t);
        if let Some(window) = app_t.upgrade() {
            reset_to_idle(&window);
        }
    });
}

/// Month ‹ / › navigation (delta = -1 / +1), clamped to the current month.
fn cal_shift(app: &Rc<App>, delta: i32) {
    {
        let mut st = app.state.borrow_mut();
        if let Some(m) = st.cal_month {
            let min = first_of_month(today());
            let nm = if delta < 0 {
                m.checked_sub_months(Months::new(1)).unwrap_or(m).max(min)
            } else {
                m.checked_add_months(Months::new(1)).unwrap_or(m)
            };
            st.cal_month = Some(nm);
        }
    }
    render_leave(app);
    arm_leave_timeout(app);
}

/// A calendar day was tapped: extend or restart the range (hotel-booking logic).
fn on_cal_day(app: &Rc<App>, day: i32) {
    if day < 1 {
        return;
    }
    let month = app.state.borrow().cal_month;
    let Some(month) = month else {
        return;
    };
    let Some(date) = month.with_day(day as u32) else {
        return;
    };
    {
        let mut st = app.state.borrow_mut();
        match (st.leave_from, st.leave_to) {
            // No start yet, or a full range already chosen → start fresh.
            (None, _) | (Some(_), Some(_)) => {
                st.leave_from = Some(date);
                st.leave_to = None;
            }
            // Start chosen → pick the end (or restart if before start).
            (Some(f), None) => {
                if date >= f {
                    st.leave_to = Some(date);
                } else {
                    st.leave_from = Some(date);
                }
            }
        }
    }
    render_leave(app);
    arm_leave_timeout(app);
}

/// "Urlaub beantragen" (submit) was pressed: file the chosen range.
fn on_leave_submit(app: &Rc<App>) {
    app.choice_timer.stop();
    let (badge, from, to) = {
        let st = app.state.borrow();
        (st.pending_badge.clone(), st.leave_from, st.leave_to)
    };
    let Some(badge) = badge else {
        return;
    };
    let Some(from) = from else {
        // Nothing selected yet — keep the calendar up.
        arm_leave_timeout(app);
        return;
    };
    let to = to.unwrap_or(from); // single day when no end was chosen

    set_busy(app);

    let client = Arc::clone(&app.client);
    let weak = app.weak.clone();
    let from_s = from.format("%Y-%m-%d").to_string();
    let to_s = to.format("%Y-%m-%d").to_string();
    spawn_worker("leave-worker", move || {
        let result = client.leave_request(&badge, &from_s, &to_s);
        let _ = slint::invoke_from_event_loop(move || {
            dispatch_leave(weak, result);
        });
    });
}

/// Continuation of [`on_leave_submit`] on the event-loop thread.
fn dispatch_leave(weak: slint::Weak<MainWindow>, result: Result<LeaveResult, ApiError>) {
    let Some(window) = weak.upgrade() else {
        return;
    };
    let Some(app) = app_from_window(&window) else {
        return;
    };
    match result {
        Ok(leave) => show_leave_done(&app, &leave),
        Err(err) => show_error(&app, &err),
    }
}

/// Render the "request filed" confirmation and return to idle after a moment.
fn show_leave_done(app: &Rc<App>, leave: &LeaveResult) {
    if let Some(window) = app.upgrade() {
        window.set_state(SharedString::from("undone"));
        window.set_heading(SharedString::from("Urlaubsantrag eingereicht"));
        let days = leave.days_count;
        let days_txt = if (days - days.round()).abs() < 0.01 {
            format!("{}", days.round() as i64)
        } else {
            format!("{days}")
        };
        let unit = if (days - 1.0).abs() < 0.01 { "Werktag" } else { "Werktage" };
        window.set_message(SharedString::from(format!(
            "{days_txt} {unit} – wartet auf Genehmigung"
        )));
        window.set_detail(SharedString::from(match leave.vacation_remaining_days {
            Some(rem) => format!("Resturlaub: {}", fmt_days_opt(Some(rem))),
            None => String::new(),
        }));
    }
    clear_pending(app);
    arm_reset(app, SUCCESS_WINDOW);
}

/// First day of the month containing `d`.
fn first_of_month(d: NaiveDate) -> NaiveDate {
    d.with_day(1).unwrap_or(d)
}

/// Number of days in the month starting at `month` (a first-of-month date).
fn days_in_month(month: NaiveDate) -> i32 {
    let next = month
        .checked_add_months(Months::new(1))
        .unwrap_or(month);
    (next - month).num_days() as i32
}

/// German month + year label, e.g. `"Oktober 2026"`.
fn month_label_de(month: NaiveDate) -> String {
    let name = [
        "Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August",
        "September", "Oktober", "November", "Dezember",
    ][(month.month0()) as usize];
    format!("{name} {}", month.year())
}

/// Today's local date (the vacation-request lower bound).
fn today() -> NaiveDate {
    Local::now().date_naive()
}

/// Format a date as `"Mo 05.10.2026"` for the leave screen.
fn fmt_de_date(d: NaiveDate) -> String {
    let wd = match d.weekday() {
        Weekday::Mon => "Mo",
        Weekday::Tue => "Di",
        Weekday::Wed => "Mi",
        Weekday::Thu => "Do",
        Weekday::Fri => "Fr",
        Weekday::Sat => "Sa",
        Weekday::Sun => "So",
    };
    format!("{wd} {}", d.format("%d.%m.%Y"))
}

/// Phase 2: record the chosen event on a worker thread, then show success.
fn commit(app: &Rc<App>, badge: &str, action: Action) {
    let Some(event_type) = action.event_type() else {
        show_error(
            app,
            &ApiError::InvalidTransition("Aktion nicht möglich".to_owned()),
        );
        return;
    };

    set_busy(app);

    let client = Arc::clone(&app.client);
    let queue = Arc::clone(&app.queue);
    let weak = app.weak.clone();
    let badge = badge.to_owned();
    let event_type = event_type.to_owned();
    spawn_worker("commit-worker", move || {
        let result = client.commit(&badge, &event_type);
        // Backend weg zwischen Vorschau und Buchung: gewaehlte Aktion samt
        // Original-Zeit puffern und als "gespeichert" bestaetigen.
        if matches!(result, Err(ApiError::Transport(_))) {
            let punch = QueuedPunch {
                client_event_id: new_client_event_id(),
                badge_id: badge.clone(),
                event_type: Some(event_type.clone()),
                event_time: Utc::now().to_rfc3339_opts(SecondsFormat::Secs, true),
            };
            queue.append(&punch);
            let _ = slint::invoke_from_event_loop(move || {
                let Some(window) = weak.upgrade() else { return };
                let Some(app) = app_from_window(&window) else { return };
                show_offline_saved(&app);
            });
            return;
        }
        let _ = slint::invoke_from_event_loop(move || {
            dispatch_commit(weak, result);
        });
    });
}

/// Continuation of [`commit`] on the event-loop thread.
fn dispatch_commit(weak: slint::Weak<MainWindow>, result: Result<PunchResult, ApiError>) {
    let Some(window) = weak.upgrade() else {
        return;
    };
    let Some(app) = app_from_window(&window) else {
        return;
    };

    match result {
        Err(err) => show_error(&app, &err),
        Ok(punch) => show_success(&app, &punch),
    }
}

/// Render the success screen.
fn show_success(app: &Rc<App>, punch: &PunchResult) {
    let first = first_name(&punch.employee_name);
    clear_pending(app);

    // Persoenliche Kiosk-Hinweise (#189): eigener, prominenter Block —
    // gleiche Optik wie die Rundmeldung (frueher an "detail" angehaengt).
    let detail = format_event_time(&punch.event_time);
    let personal = punch.personal_messages.join("\n");

    if let Some(window) = app.upgrade() {
        window.set_state(SharedString::from("ok"));
        window.set_heading(SharedString::from(punch.action.heading_de(first)));
        window.set_message(SharedString::from(punch.action.label_de()));
        window.set_detail(SharedString::from(detail));
        window.set_personal_message(SharedString::from(personal));
    }

    arm_reset(app, SUCCESS_WINDOW);
}

/// Switch the UI into the busy state.
fn set_busy(app: &Rc<App>) {
    if let Some(window) = app.upgrade() {
        window.set_state(SharedString::from("busy"));
        window.set_heading(SharedString::from("Zeiterfassung"));
        window.set_message(SharedString::from("Einen Moment…"));
        window.set_detail(SharedString::from(""));
    }
}

/// Render a typed [`ApiError`] onto the window and return to idle after a short
/// notice.
/// Bestaetigungsbild fuer einen lokal gepufferten Stempel (Backend offline).
///
/// Bewusst als Erfolg dargestellt: der Mitarbeiter hat gestempelt, die Buchung
/// ist sicher gespeichert und wird automatisch nachgereicht.
fn show_offline_saved(app: &Rc<App>) {
    clear_pending(app);
    let pending = app.queue.len();
    if let Some(window) = app.upgrade() {
        window.set_state(SharedString::from("ok"));
        window.set_heading(SharedString::from("Gespeichert!"));
        window.set_message(SharedString::from(
            "Wird automatisch als Kommen/Gehen verbucht, sobald die Verbindung wieder da ist.",
        ));
        window.set_detail(SharedString::from(Local::now().format("um %H:%M Uhr").to_string()));
        window.set_queue_info(SharedString::from(queue_info_text(pending)));
    }
    arm_reset(app, SUCCESS_WINDOW);
}

/// Text der Idle-Statuszeile fuer `pending` wartende Offline-Stempel.
fn queue_info_text(pending: usize) -> String {
    match pending {
        0 => String::new(),
        1 => "1 Stempel wartet auf Nachreichung".to_owned(),
        n => format!("{n} Stempel warten auf Nachreichung"),
    }
}

/// Statuszeile vom Flush-Thread aus aktualisieren (marshalled auf den
/// Event-Loop-Thread).
fn update_queue_info(weak: &slint::Weak<MainWindow>, pending: usize) {
    let text = queue_info_text(pending);
    let _ = weak.upgrade_in_event_loop(move |window| {
        window.set_queue_info(SharedString::from(text));
    });
}

/// Alle wartenden Eintraege der Reihe nach nachreichen.
///
/// Fehler werden über [`ApiError::is_transient`] klassifiziert:
/// - **Transient** (Transport, 5xx, 429): das Backend ist gerade nicht
///   gesund/erreichbar, nicht der Eintrag ist ungültig. Der Durchlauf bricht
///   ab (weitere Versuche in dieser Runde wären ebenso sinnlos) und der
///   Eintrag bleibt in der Queue für den nächsten Flush.
/// - **Permanent** (404/409/422/…): das Backend hat GENAU diesen Eintrag
///   geprüft und dauerhaft abgelehnt (z. B. unbekannter Ausweis). So ein
///   Eintrag wird nicht mehr erneut versucht, aber auch nicht stillschweigend
///   verworfen — er landet über [`OfflineQueue::discard`] in der
///   Dead-Letter-Datei, damit eine echte Stempelung nachvollziehbar bleibt.
fn flush_queue(client: &BackendClient, queue: &OfflineQueue) -> usize {
    for punch in queue.load() {
        match client.replay(&punch) {
            Ok(()) => {
                eprintln!("[queue] nachgereicht: {}", punch.client_event_id);
                queue.remove(&punch.client_event_id);
            }
            Err(e) if e.is_transient() => {
                eprintln!(
                    "[queue] Backend vorübergehend nicht verfügbar, warte: {e} \
                     (Eintrag {} bleibt in der Queue)",
                    punch.client_event_id
                );
                break;
            }
            Err(e) => {
                eprintln!(
                    "[queue] Eintrag {} endgueltig abgelehnt: {e}",
                    punch.client_event_id
                );
                queue.discard(&punch, &e.to_string());
            }
        }
    }
    queue.len()
}

fn show_error(app: &Rc<App>, err: &ApiError) {
    clear_pending(app);
    if let Some(window) = app.upgrade() {
        window.set_state(SharedString::from("error"));
        window.set_heading(SharedString::from("Fehler"));
        window.set_message(SharedString::from(err.to_string()));
        window.set_detail(SharedString::from(""));
    }
    arm_reset(app, NOTICE_WINDOW);
}

/// Render the Unpaired-Fob screen (bead 2hv) for a tap that authenticated
/// but resolved to no employee (HTTP 404 preview): shows ONLY the chip UID,
/// formatted like NXP TagInfo on a phone, so an operator can read it off
/// the terminal and assign it to an employee in the web app.
///
/// Deliberately renders NO token/badge id and no key material — the token is
/// an internal credential and does not belong on a wall terminal.
fn show_unpaired(app: &Rc<App>, uid: &str) {
    clear_pending(app);
    if let Some(window) = app.upgrade() {
        window.set_state(SharedString::from("unpaired"));
        window.set_heading(SharedString::from("Karte nicht zugewiesen"));
        window.set_message(SharedString::from("Noch nicht registriert"));
        window.set_detail(SharedString::from("UID in der Verwaltung zuweisen"));
        window.set_chip_uid(SharedString::from(format_uid_display(uid)));
    }
    arm_reset(app, UNPAIRED_WINDOW);
}

/// Format a hex chip UID the way NXP TagInfo shows it on a phone: uppercase,
/// space-separated byte pairs (e.g. `"045f43aa6a1a90"` -> `"04 5F 43 AA 6A
/// 1A 90"`). Non-hex/odd-length input is tolerated (best-effort — the last
/// group may be a single character) rather than panicking, since this only
/// feeds a display string.
fn format_uid_display(uid_hex: &str) -> String {
    let upper = uid_hex.to_ascii_uppercase();
    let bytes = upper.as_bytes();
    bytes
        .chunks(2)
        .map(|chunk| std::str::from_utf8(chunk).unwrap_or(""))
        .collect::<Vec<_>>()
        .join(" ")
}

/// Arm the automatic return-to-idle after `delay`, clearing any pending badge
/// once it fires.
fn arm_reset(app: &Rc<App>, delay: Duration) {
    let app_t = Rc::clone(app);
    app.reset_timer.start(TimerMode::SingleShot, delay, move || {
        clear_pending(&app_t);
        // If a transient screen (e.g. an unrelated badge's error notice)
        // interrupted an active personalize-mode session — the only way
        // `mode` can still be `Personalize` here is a stray tap that
        // resolved to neither an admin nor an employee fob while
        // personalize mode was on — return to the personalize screen
        // instead of idle, rather than silently dropping the operator out
        // of the mode they're still in.
        if app_t.mode.borrow().is_personalize() {
            render_personalize_screen(&app_t);
            return;
        }
        if let Some(window) = app_t.upgrade() {
            reset_to_idle(&window);
        }
    });
}

/// Reset the window back to the idle prompt.
fn reset_to_idle(window: &MainWindow) {
    window.set_state(SharedString::from("idle"));
    window.set_heading(SharedString::from("Zeiterfassung"));
    window.set_message(SharedString::from("Bitte Ausweis an das Lesegerät halten"));
    window.set_detail(SharedString::from(""));
    // Persoenliche Meldung gehoert nur zum Erfolgs-Screen des jeweiligen
    // Mitarbeiters — nie in den Ruhebildschirm uebernehmen.
    window.set_personal_message(SharedString::from(""));
}

/// Recover the [`App`] from the window so worker continuations (which can only
/// carry a `Weak<MainWindow>`) can reach the shared state and timers.
///
/// The `App` is stashed once at startup in a thread-local keyed by the window's
/// identity; all event-loop closures run on the same thread, so this is sound.
fn app_from_window(_window: &MainWindow) -> Option<Rc<App>> {
    APP.with(|cell| cell.borrow().clone())
}

thread_local! {
    /// The single live [`App`], set once on the event-loop thread at startup.
    static APP: RefCell<Option<Rc<App>>> = const { RefCell::new(None) };
}

/// Spawn a worker thread for a network call, surfacing a spawn failure on the
/// UI directly.
fn spawn_worker<F>(name: &str, work: F)
where
    F: FnOnce() + Send + 'static,
{
    if let Err(e) = thread::Builder::new()
        .name(name.to_owned())
        .spawn(work)
    {
        let msg = e.to_string();
        let _ = slint::invoke_from_event_loop(move || {
            APP.with(|cell| {
                if let Some(app) = cell.borrow().clone() {
                    show_error(
                        &app,
                        &ApiError::Transport(format!("Worker-Thread fehlgeschlagen: {msg}")),
                    );
                }
            });
        });
    }
}

/// Clean up a backend full name for the profile heading: drop a trailing
/// `(Demo)` marker and collapse the surrounding whitespace.
fn display_name(full: &str) -> String {
    full.replace("(Demo)", "")
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
}

/// Format a non-negative minute count as `"Xh YYm"` (e.g. `"8h 05m"`).
fn fmt_hm(minutes: i64) -> String {
    let m = minutes.abs();
    format!("{}h {:02}m", m / 60, m % 60)
}

/// Format a signed minute count as `"+Xh YYm"` / `"-Xh YYm"`, or `"0h 00m"` for
/// zero. Used for the overtime and Arbeitszeitkonto rows.
fn fmt_hm_signed(minutes: i64) -> String {
    if minutes == 0 {
        return "0h 00m".to_owned();
    }
    let sign = if minutes < 0 { "-" } else { "+" };
    let m = minutes.abs();
    format!("{sign}{}h {:02}m", m / 60, m % 60)
}

/// Format a German day count, dropping a `,0` fraction (`18.5` → `"18,5"`,
/// `24.0` → `"24"`).
fn fmt_days(days: f64) -> String {
    let s = format!("{days:.1}");
    let s = s.strip_suffix(".0").map_or(s.clone(), str::to_owned);
    s.replace('.', ",")
}

/// Format the remaining-vacation row, or a dash when unavailable.
fn fmt_days_opt(days: Option<f64>) -> String {
    match days {
        Some(d) => format!("{} Tage", fmt_days(d)),
        None => "—".to_owned(),
    }
}

/// Format the contracted-hours row from the backend's decimal string
/// (`"40.00"` → `"40 h/Woche"`, `"37.50"` → `"37,5 h/Woche"`), or a dash.
fn fmt_contract(hours: Option<&str>) -> String {
    match hours {
        Some(h) if !h.trim().is_empty() => {
            let nice = match h.trim().split_once('.') {
                Some((int, frac)) => {
                    let frac = frac.trim_end_matches('0');
                    if frac.is_empty() {
                        int.to_owned()
                    } else {
                        format!("{int},{frac}")
                    }
                }
                None => h.trim().to_owned(),
            };
            format!("{nice} h/Woche")
        }
        _ => "—".to_owned(),
    }
}

/// Best-effort extraction of `HH:MM` from an ISO-8601 timestamp for display.
///
/// Returns `"um HH:MM Uhr"` when the time can be parsed, or an empty string when
/// it cannot (the detail line is hidden when empty).
fn format_event_time(iso: &str) -> String {
    use chrono::DateTime;

    if let Ok(dt) = DateTime::parse_from_rfc3339(iso) {
        // In Lokalzeit anzeigen — der Server liefert UTC.
        return format!("um {} Uhr", dt.with_timezone(&Local).format("%H:%M"));
    }
    // Fall back to a tolerant manual parse (handles a trailing `Z` or missing
    // offset that `parse_from_rfc3339` rejects).
    if let Some((_, rest)) = iso.split_once('T') {
        let time = rest.split(['.', '+', '-', 'Z']).next().unwrap_or(rest);
        let mut parts = time.split(':');
        if let (Some(h), Some(m)) = (parts.next(), parts.next()) {
            if h.len() == 2 && m.len() == 2 {
                return format!("um {h}:{m} Uhr");
            }
        }
    }
    String::new()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn formats_iso_timestamp_to_hh_mm() {
        // Anzeige ist LOKALZEIT — erwartete Werte im Test mitkonvertieren,
        // damit er in jeder Zeitzone (Pi: Europe/Berlin, CI: UTC) besteht.
        use chrono::DateTime;
        let expect = |iso: &str| {
            let dt = DateTime::parse_from_rfc3339(iso).unwrap();
            format!("um {} Uhr", dt.with_timezone(&Local).format("%H:%M"))
        };
        assert_eq!(
            format_event_time("2026-06-24T13:45:01.123456+00:00"),
            expect("2026-06-24T13:45:01.123456+00:00")
        );
        assert_eq!(
            format_event_time("2026-06-24T08:00:00Z"),
            expect("2026-06-24T08:00:00+00:00")
        );
        assert_eq!(
            format_event_time("2026-06-24T17:05:00+02:00"),
            expect("2026-06-24T17:05:00+02:00")
        );
    }

    #[test]
    fn unparseable_time_yields_empty() {
        assert_eq!(format_event_time("not-a-time"), "");
    }

    #[test]
    fn formats_minutes_unsigned() {
        assert_eq!(fmt_hm(0), "0h 00m");
        assert_eq!(fmt_hm(5), "0h 05m");
        assert_eq!(fmt_hm(485), "8h 05m");
    }

    #[test]
    fn formats_minutes_signed() {
        assert_eq!(fmt_hm_signed(0), "0h 00m");
        assert_eq!(fmt_hm_signed(90), "+1h 30m");
        assert_eq!(fmt_hm_signed(-45), "-0h 45m");
        assert_eq!(fmt_hm_signed(-750), "-12h 30m");
    }

    #[test]
    fn formats_german_days() {
        assert_eq!(fmt_days(24.0), "24");
        assert_eq!(fmt_days(18.5), "18,5");
        assert_eq!(fmt_days_opt(Some(18.5)), "18,5 Tage");
        assert_eq!(fmt_days_opt(None), "—");
    }

    #[test]
    fn formats_contract_hours() {
        assert_eq!(fmt_contract(Some("40.00")), "40 h/Woche");
        assert_eq!(fmt_contract(Some("37.50")), "37,5 h/Woche");
        assert_eq!(fmt_contract(None), "—");
        assert_eq!(fmt_contract(Some("")), "—");
    }

    #[test]
    fn cleans_demo_name() {
        assert_eq!(display_name("Max (Demo)"), "Max");
        assert_eq!(display_name("Anna Müller"), "Anna Müller");
    }

    #[test]
    fn formats_uid_taginfo_style() {
        assert_eq!(format_uid_display("045f43aa6a1a90"), "04 5F 43 AA 6A 1A 90");
        assert_eq!(format_uid_display("DEADBEEF"), "DE AD BE EF");
        assert_eq!(format_uid_display(""), "");
    }

    #[test]
    fn formats_uid_odd_length_best_effort() {
        // Shouldn't happen for a real 7-byte UID, but must not panic.
        assert_eq!(format_uid_display("abc"), "AB C");
    }
}
