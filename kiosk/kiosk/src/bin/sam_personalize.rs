//! SSH-run CLI for DESFire fob personalization (bead 4ty).
//!
//! Run on the Pi (or any host with PC/SC SAM+PICC readers) by a technician
//! doing a pilot fob-provisioning batch. All hardware-independent logic
//! lives in `reader::sam::personalize` (unit-tested against mocks, no
//! hardware needed); this file is a thin CLI shell around it plus the
//! shared `api`/`config` modules (reused from the main kiosk binary,
//! brought in via `#[path]` since this crate has no `lib.rs` — the same
//! pattern `sam_ceremony.rs` uses for `reader`).
//!
//! ## Subcommands
//!
//! - `sam_personalize run` — the real batch: for each of `--count` fobs,
//!   wait for a tap, personalize it (auth default key -> create app ->
//!   diversify+write app key -> create+write token-id file), then
//!   batch-register every successfully personalized token-id with the
//!   backend (`POST /nfc/register-to-pool`). Requires `--features
//!   real-reader` (PC/SC + `libpcsclite`); without it, prints an
//!   explanatory error and exits non-zero.
//! - `sam_personalize self-test` — offline: runs the personalization
//!   pipeline against in-memory mock SAM+PICC transports, so a technician
//!   can tell "the software is broken" apart from "the hardware/wiring is
//!   broken" when triaging a failure over SSH. No hardware, no real key
//!   material touched, no backend call.
//!
//! Every subcommand logs each step verbosely (`[personalize] ...`) to
//! stdout (progress) / stderr (failures), so a technician can paste the
//! exact failing step when asking for help.
//!
//! Fail-closed, per fob: any unexpected SAM/PICC status aborts that fob's
//! personalization immediately. A failed fob is **never** included in the
//! backend `register-to-pool` batch, but the batch continues to the next
//! fob (so one bad tap in a ~50-fob run doesn't require restarting from
//! scratch) — the final summary reports exactly which fobs succeeded and
//! which failed. There is no partial-success path that looks like success.

#[path = "../api.rs"]
mod api;
#[path = "../config.rs"]
mod config;
#[path = "../queue.rs"]
mod queue;
#[path = "../reader/mod.rs"]
mod reader;

use std::collections::HashMap;
use std::env;
use std::process::ExitCode;

// `BackendClient`/`Config` are only reachable from `cmd_run`'s real-hardware
// path (the `self-test` subcommand never touches the network) — gate the
// imports the same way so a default (non-`real-reader`) build doesn't warn.
#[cfg(feature = "real-reader")]
use api::BackendClient;
#[cfg(feature = "real-reader")]
use config::Config;
use reader::sam::apdu;
use reader::sam::crypto;
use reader::sam::error::SamError;
use reader::sam::mock::MockTransport;
use reader::sam::personalize::{self, default_personalize_config, FobKeys, PersonalizedFob};

fn main() -> ExitCode {
    let args: Vec<String> = env::args().collect();
    let result = match args.get(1).map(String::as_str) {
        Some("run") => cmd_run(&args[2..]),
        Some("self-test") => cmd_self_test(),
        Some(other) => {
            eprintln!("[personalize] unknown subcommand {other:?}\n");
            print_usage();
            return ExitCode::FAILURE;
        }
        None => {
            print_usage();
            return ExitCode::FAILURE;
        }
    };

    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(msg) => {
            eprintln!("[personalize] FAILED: {msg}");
            ExitCode::FAILURE
        }
    }
}

fn print_usage() {
    eprintln!(
        "Usage:\n\
         \x20 sam_personalize run --sam <reader-name-substring> --picc <reader-name-substring> \\\n\
         \x20     --company-key-entry-no N [--company-key-entry-ver N] \\\n\
         \x20     --aid <6 hex chars> [--app-key-no N] [--file-no N] --count N [--admin]\n\
         \x20     (requires a build with --features real-reader; reads\n\
         \x20      KIOSK_BACKEND_URL / KIOSK_TENANT_ID / KIOSK_DEVICE_KEY from the environment,\n\
         \x20      same as the main kiosk binary; --admin mints admin fob(s) for the wall\n\
         \x20      terminal's personalize mode instead of pooling employee-badge candidates)\n\
         \x20\n\
         \x20     --company-key-entry-no/--company-key-entry-ver: the SAM key entry\n\
         \x20     holding the company master key (loaded by the key-loading ceremony,\n\
         \x20     bead 3pn) — diversified via SAM_DumpSecretKey for each fob's app key.\n\
         \x20     As of the 2026-07-15 rework (bead 4ty), personalization authenticates\n\
         \x20     to each fresh application's factory-default (all-zero AES) key 0\n\
         \x20     ENTIRELY IN SOFTWARE, so there is no longer a `--default-key-entry-*`\n\
         \x20     flag — no SAM key entry is needed for that step at all.\n\
         \x20 sam_personalize self-test\n"
    );
}

fn fmt_err(e: SamError) -> String {
    e.to_string()
}

// ---------------------------------------------------------------------
// Minimal manual arg parsing (mirrors sam_ceremony.rs's ParsedArgs — this
// tool has exactly two subcommands and a handful of `--flag value` pairs,
// so a small CLI-parsing dependency wasn't worth adding).
// ---------------------------------------------------------------------

/// Flags that take no value (presence = `true`) — everything else must be a
/// `--flag value` pair.
const BOOLEAN_FLAGS: &[&str] = &["admin"];

struct ParsedArgs {
    flags: HashMap<String, Vec<String>>,
    /// Presence-only flags (see [`BOOLEAN_FLAGS`]) that were passed.
    switches: std::collections::HashSet<String>,
}

impl ParsedArgs {
    fn parse(args: &[String]) -> Result<Self, String> {
        let mut flags: HashMap<String, Vec<String>> = HashMap::new();
        let mut switches: std::collections::HashSet<String> = std::collections::HashSet::new();
        let mut i = 0;
        while i < args.len() {
            let a = &args[i];
            let Some(name) = a.strip_prefix("--") else {
                return Err(format!(
                    "unexpected positional argument: {a:?} (all arguments must be --flag value pairs)"
                ));
            };
            if BOOLEAN_FLAGS.contains(&name) {
                switches.insert(name.to_string());
                i += 1;
                continue;
            }
            let value = args
                .get(i + 1)
                .cloned()
                .ok_or_else(|| format!("--{name} is missing its value"))?;
            flags.entry(name.to_string()).or_default().push(value);
            i += 2;
        }
        Ok(Self { flags, switches })
    }

    fn one(&self, name: &str) -> Option<&str> {
        self.flags
            .get(name)
            .and_then(|v| v.first())
            .map(String::as_str)
    }

    /// Whether a presence-only flag (see [`BOOLEAN_FLAGS`]) was passed.
    fn flag(&self, name: &str) -> bool {
        self.switches.contains(name)
    }

    fn require_one(&self, name: &str) -> Result<&str, String> {
        self.one(name)
            .ok_or_else(|| format!("--{name} is required"))
    }

    fn one_u8(&self, name: &str, default: u8) -> Result<u8, String> {
        match self.one(name) {
            Some(v) => v
                .parse::<u8>()
                .map_err(|e| format!("--{name} must be a number 0-255: {e}")),
            None => Ok(default),
        }
    }

    fn require_u8(&self, name: &str) -> Result<u8, String> {
        self.require_one(name)?
            .parse::<u8>()
            .map_err(|e| format!("--{name} must be a number 0-255: {e}"))
    }

    fn require_usize(&self, name: &str) -> Result<usize, String> {
        self.require_one(name)?
            .parse::<usize>()
            .map_err(|e| format!("--{name} must be a positive number: {e}"))
    }
}

fn parse_aid_hex(hex_str: &str) -> Result<[u8; 3], String> {
    let bytes = hex::decode(hex_str.trim()).map_err(|e| format!("--aid: not valid hex: {e}"))?;
    if bytes.len() != 3 {
        return Err(format!(
            "--aid: must decode to exactly 3 bytes (6 hex chars), got {}",
            bytes.len()
        ));
    }
    let mut out = [0u8; 3];
    out.copy_from_slice(&bytes);
    Ok(out)
}

/// Pure collection step: given N already-computed personalization results
/// (as would come from N successful `personalize_fob` calls), collect their
/// token-id hex strings for the batch backend registration call. Split out
/// from the interactive per-fob loop so it is unit-testable without any
/// hardware.
fn collect_token_ids(results: &[PersonalizedFob]) -> Vec<String> {
    results.iter().map(|r| r.token_id_hex.clone()).collect()
}

/// Same collection step as [`collect_token_ids`], but for the chip UIDs
/// (bead rst) — parallel-indexed to it so `register_to_pool`'s `uids`
/// argument lines up with `token_ids`. Every batch-personalized fob has a
/// known UID (unlike the kiosk's 404-backfill path, which may not), so every
/// entry here is `Some`.
fn collect_uids(results: &[PersonalizedFob]) -> Vec<Option<String>> {
    results.iter().map(|r| Some(r.uid_hex.clone())).collect()
}

// ---------------------------------------------------------------------
// `sam_personalize self-test`
// ---------------------------------------------------------------------

fn cmd_self_test() -> Result<(), String> {
    println!(
        "[personalize] self-test: exercising the personalization pipeline against in-memory mock \
         SAM+PICC transports (no hardware touched, no backend call made)."
    );

    let uid = [0x04, 0x11, 0x22, 0x33, 0x44, 0x55, 0x66];
    let cfg = default_personalize_config(
        [0x01, 0x02, 0x03],
        0x00,
        0x00,
        FobKeys {
            company_key_entry_no: 0x02,
            company_key_entry_ver: 0x00,
        },
    );

    let token_id = personalize::generate_token_id().map_err(fmt_err)?;
    println!(
        "[personalize]  - generated a synthetic token-id via CSPRNG (not written to a real card)"
    );

    // Fixed (not CSPRNG) challenge for the self-test — deterministic so the
    // mocks below can be built self-consistently ahead of time. A real run
    // (`cmd_run`) uses `personalize::generate_personalize_challenge()`.
    let challenge = personalize::PersonalizeChallenge {
        host_rnd1: [0x01u8; 12],
        host_rnd_a: [0x02u8; 16],
        ev2_rnd_a: [0x03u8; 16],
        ev2_rnd_a2: [0x04u8; 16],
    };
    let diversified_key = [0x5Au8; 16];

    let (mut sam, mut picc) = self_test_mocks(&challenge, diversified_key);
    let result = personalize::personalize_fob(&mut sam, &mut picc, uid, &cfg, token_id, challenge)
        .map_err(fmt_err)?;
    println!(
        "[personalize]  - personalization pipeline ran end-to-end against the mock transports \
         (token_id={}, uid={})",
        result.token_id_hex, result.uid_hex
    );

    let batch_ids = collect_token_ids(std::slice::from_ref(&result));
    if batch_ids != vec![result.token_id_hex.clone()] {
        return Err(
            "collect_token_ids did not preserve the single personalized fob's token-id — this is \
             a real software bug, do NOT proceed to a real run until this self-test passes"
                .to_string(),
        );
    }
    println!("[personalize]  - batch token-id collection OK");

    // bead rst: same pairing check for the chip-UID collection step used by
    // `cmd_run`'s `register_to_pool(&token_ids, &uids)` call.
    let batch_uids = collect_uids(std::slice::from_ref(&result));
    if batch_uids != vec![Some(result.uid_hex.clone())] {
        return Err(
            "collect_uids did not preserve the single personalized fob's chip UID — this is a \
             real software bug, do NOT proceed to a real run until this self-test passes"
                .to_string(),
        );
    }
    println!("[personalize]  - batch UID collection OK");

    println!(
        "[personalize] self-test PASSED. The software pipeline (software EV2 auth, \
         CreateApplication, SelectApplication, SAM_DumpSecretKey + software ChangeKey, \
         CreateStdDataFile, WriteData) is sound. If a real run still fails, the problem is most \
         likely the SAM/PICC hardware, the PC/SC reader/wiring, or the reader-name substrings you \
         passed to --sam/--picc — not this code."
    );
    Ok(())
}

/// Build mock SAM+PICC transports that correctly play out one full,
/// successful `personalize_fob` run — genuinely crypto-self-consistent
/// (proves wiring, not hardware correctness), since the 2026-07-15 rework's
/// software EV2 auth actually verifies the card's RndA' response.
///
/// Mirrors `reader::sam::personalize::tests::push_full_happy_path`'s
/// approach, but is deliberately **not** shared with it via `#[cfg(test)]`
/// — this function backs a real, non-test CLI subcommand (`self-test`)
/// exposed to technicians, so it stays independent of the unit-test module
/// it happens to resemble.
fn self_test_mocks(
    challenge: &personalize::PersonalizeChallenge,
    diversified_key: [u8; 16],
) -> (MockTransport, MockTransport) {
    let mut sam = MockTransport::new();
    let mut picc = MockTransport::new();

    // Steps 1-3: SelectApplication(000000), CreateApplication,
    // SelectApplication(AID) — all plain/unauthenticated, sent straight to
    // the card (no SAM session exists yet). Response is just the PICC_OK
    // status byte.
    picc.push_ok(vec![apdu::PICC_OK]); // Step 1: SelectApplication(000000).
    picc.push_ok(vec![apdu::PICC_OK]); // Step 2: CreateApplication.
    picc.push_ok(vec![apdu::PICC_OK]); // Step 3: SelectApplication(AID).

    // Step 3.5: SAM_AuthenticateHost (self-consistent — arbitrary rnd2/
    // sam_rnd_b, framing/sequencing wiring only, not hardware correctness).
    let host_master_key = [0u8; 16];
    let rnd2 = [0x77u8; 12];
    let sam_rnd_b = [0x88u8; 16];
    let mut resp1 = rnd2.to_vec();
    resp1.push(0x90);
    resp1.push(0xAF);
    sam.push_ok(resp1);
    let kex = crypto::derive_kex(&host_master_key, &challenge.host_rnd1, &rnd2).unwrap();
    let rnd_bc = crypto::aes128_cbc_encrypt_no_pad(&kex, &[0u8; 16], &sam_rnd_b).unwrap();
    let mut resp2 = vec![0u8; 8];
    resp2.extend_from_slice(&rnd_bc);
    resp2.push(0x90);
    resp2.push(0xAF);
    sam.push_ok(resp2);
    sam.push_ok(vec![0x90, 0x00]);

    // Step 4: software EV2-First handshake against the factory all-zero AES
    // app key — the SUT actually decrypts/verifies these bytes, so they
    // must be genuinely correctly encrypted, not arbitrary filler.
    let app_key = [0u8; 16];
    let card_rnd_b = [0x99u8; 16];
    let ek_rnd_b = crypto::aes128_cbc_encrypt_no_pad(&app_key, &[0u8; 16], &card_rnd_b).unwrap();
    let mut ev2_resp1 = vec![apdu::PICC_AF];
    ev2_resp1.extend_from_slice(&ek_rnd_b);
    picc.push_ok(ev2_resp1);

    let rnd_b_rot = crypto::rotate_left_16(&card_rnd_b, 1);
    let mut plain4 = Vec::with_capacity(32);
    plain4.extend_from_slice(&challenge.ev2_rnd_a);
    plain4.extend_from_slice(&rnd_b_rot);
    let _cipher4 = crypto::aes128_cbc_encrypt_no_pad(&app_key, &[0u8; 16], &plain4).unwrap();

    // DESFire EV2 resets the IV to zero per handshake message — response IV=0.
    let ti = [0xAAu8, 0xBB, 0xCC, 0xDD];
    let rnd_a_rot = crypto::rotate_left_16(&challenge.ev2_rnd_a, 1);
    let mut plain5 = Vec::with_capacity(32);
    plain5.extend_from_slice(&ti);
    plain5.extend_from_slice(&rnd_a_rot);
    plain5.extend_from_slice(&[0u8; 12]); // PDcap2 || PCDcap2
    let cipher5 = crypto::aes128_cbc_encrypt_no_pad(&app_key, &[0u8; 16], &plain5).unwrap();
    let mut ev2_resp2 = vec![apdu::PICC_OK];
    ev2_resp2.extend_from_slice(&cipher5);
    picc.push_ok(ev2_resp2);

    // Step 5: SAM_DumpSecretKey (Full-protection round trip) + native
    // ChangeKey to the card.
    let host_session =
        crypto::derive_host_session_keys(&host_master_key, &challenge.host_rnd_a, &sam_rnd_b)
            .unwrap();
    sam.push_ok(mock_full_protection_response(
        &host_session.ke,
        &host_session.km,
        &diversified_key,
        [0x90, 0x00],
    ));
    picc.push_ok(vec![apdu::PICC_OK]); // native ChangeKey.

    // Step 5.5: software EV2-First RE-authentication with the NEW diversified
    // key (the self-ChangeKey invalidated the step-4 session). Same
    // self-consistent handshake shape as step 4, keyed by `diversified_key`.
    let reauth_key = diversified_key;
    let card_rnd_b2 = [0x77u8; 16];
    let ek_rnd_b2 = crypto::aes128_cbc_encrypt_no_pad(&reauth_key, &[0u8; 16], &card_rnd_b2).unwrap();
    let mut reauth_resp1 = vec![apdu::PICC_AF];
    reauth_resp1.extend_from_slice(&ek_rnd_b2);
    picc.push_ok(reauth_resp1);

    let ti2 = [0xAAu8, 0xBB, 0xCC, 0xDD];
    let rnd_a2_rot = crypto::rotate_left_16(&challenge.ev2_rnd_a2, 1);
    let mut plain_reauth = Vec::with_capacity(32);
    plain_reauth.extend_from_slice(&ti2);
    plain_reauth.extend_from_slice(&rnd_a2_rot);
    plain_reauth.extend_from_slice(&[0u8; 12]); // PDcap2 || PCDcap2
    let cipher_reauth =
        crypto::aes128_cbc_encrypt_no_pad(&reauth_key, &[0u8; 16], &plain_reauth).unwrap();
    let mut reauth_resp2 = vec![apdu::PICC_OK];
    reauth_resp2.extend_from_slice(&cipher_reauth);
    picc.push_ok(reauth_resp2);

    // Step 6a/6b: CreateStdDataFile (Plain) + WriteData (Full) — both go
    // straight to the card now (no Apply_SM/Remove_SM: the SAM never
    // tracks a PICC session in this design).
    picc.push_ok(vec![apdu::PICC_OK]); // CreateStdDataFile.
    picc.push_ok(vec![apdu::PICC_OK]); // WriteData.

    (sam, picc)
}

/// Build a Full-protection SAM response the way a real SAM would — mirrors
/// `reader::sam::crypto::tests::mock_sam_full_protection_response` (private
/// to that test module); duplicated here (not `#[cfg(test)]`) because this
/// CLI's `self-test` subcommand needs it at runtime, not just in `cargo
/// test`.
fn mock_full_protection_response(
    ke: &[u8; 16],
    km: &[u8; 16],
    plain_data: &[u8],
    sw: [u8; 2],
) -> Vec<u8> {
    // Real AV3 scheme: response at CmdCtr=1 (request 0, +1), zero IVs,
    // big-endian counter, MAC = truncate_odd(CMAC(Km, IV=0, SW || CmdCtr_BE ||
    // encData)).
    let ctr_be = 1u32.to_be_bytes();

    let mut padded = plain_data.to_vec();
    padded.push(0x80);
    while padded.len() % 16 != 0 {
        padded.push(0x00);
    }
    let mut my_iv = vec![0x02u8; 4];
    for _ in 1..4 {
        my_iv.extend_from_slice(&ctr_be);
    }
    let iv: [u8; 16] = crypto::aes128_cbc_encrypt_no_pad(ke, &[0u8; 16], &my_iv)
        .unwrap()
        .try_into()
        .unwrap();
    let enc_data = crypto::aes128_cbc_encrypt_no_pad(ke, &iv, &padded).unwrap();

    let mut mac_input = Vec::new();
    mac_input.extend_from_slice(&sw);
    mac_input.extend_from_slice(&ctr_be);
    mac_input.extend_from_slice(&enc_data);
    let mac8 = crypto::truncate_mac_odd(&crypto::cmac_aes128_with_iv(km, &[0u8; 16], &mac_input));

    let mut wire = enc_data;
    wire.extend_from_slice(&mac8);
    wire.extend_from_slice(&sw);
    wire
}

// ---------------------------------------------------------------------
// `sam_personalize run` — real-reader gated.
// ---------------------------------------------------------------------

#[cfg(not(feature = "real-reader"))]
fn cmd_run(_args: &[String]) -> Result<(), String> {
    Err(
        "this binary was built without the `real-reader` feature, so it has no PC/SC support \
         and cannot talk to real SAM/PICC readers. Rebuild with: cargo build --release --features \
         real-reader (run `sam_personalize self-test` in the meantime to sanity-check the software \
         pipeline)"
            .to_string(),
    )
}

#[cfg(feature = "real-reader")]
fn cmd_run(args: &[String]) -> Result<(), String> {
    use pcsc::{Context, Protocols, Scope, ShareMode, MAX_BUFFER_SIZE};

    use reader::sam::transport::{
        find_reader_by_name_substring, PiccPcscTransport, SamPcscTransport,
    };

    let parsed = ParsedArgs::parse(args)?;

    let sam_target = parsed.require_one("sam")?.to_owned();
    let picc_target = parsed.require_one("picc")?.to_owned();
    let company_key_entry_no = parsed.require_u8("company-key-entry-no")?;
    let company_key_entry_ver = parsed.one_u8("company-key-entry-ver", 0)?;
    let aid = parse_aid_hex(parsed.require_one("aid")?)?;
    let app_key_no = parsed.one_u8("app-key-no", 0)?;
    let file_no = parsed.one_u8("file-no", 0)?;
    let count = parsed.require_usize("count")?;
    if count == 0 {
        return Err("--count must be >= 1".to_string());
    }
    let admin = parsed.flag("admin");

    let config = Config::from_env().map_err(|e| format!("environment config: {e}"))?;
    let backend = BackendClient::new(&config).map_err(|e| format!("backend client: {e}"))?;

    let cfg = default_personalize_config(
        aid,
        app_key_no,
        file_no,
        FobKeys {
            company_key_entry_no,
            company_key_entry_ver,
        },
    );

    println!(
        "[personalize] plan: personalize {count} fob(s){}, AID={:02X}{:02X}{:02X}, app-key-no={app_key_no}, file-no={file_no}",
        if admin { " (ADMIN fob(s) — wall-terminal personalize-mode gate)" } else { "" },
        aid[0], aid[1], aid[2]
    );

    let ctx = Context::establish(Scope::User)
        .map_err(|e| format!("PC/SC: cannot establish context: {e}"))?;

    let mut succeeded: Vec<PersonalizedFob> = Vec::new();
    let mut failed: Vec<(usize, String)> = Vec::new();

    for i in 1..=count {
        println!("[personalize] --- fob {i}/{count}: tap now ---");
        let outcome: Result<PersonalizedFob, String> = (|| {
            let sam_reader = find_reader_by_name_substring(&ctx, &sam_target)
                .ok_or_else(|| format!("no PC/SC reader name contains {sam_target:?} (SAM)"))?;
            let picc_reader = find_reader_by_name_substring(&ctx, &picc_target)
                .ok_or_else(|| format!("no PC/SC reader name contains {picc_target:?} (PICC)"))?;

            let mut sam_transport = SamPcscTransport::open_named(&ctx, &sam_reader)
                .map_err(|e| format!("opening SAM reader {sam_reader:?}: {e}"))?;

            let uid = wait_for_card_and_read_uid(&ctx, &picc_reader)?;
            println!("[personalize]  - detected fob, UID={}", hex::encode(uid));

            let mut picc_transport = PiccPcscTransport::open_named(&ctx, &picc_reader)
                .map_err(|e| format!("opening PICC reader {picc_reader:?}: {e}"))?;

            let token_id = personalize::generate_token_id().map_err(fmt_err)?;
            let challenge = personalize::generate_personalize_challenge().map_err(fmt_err)?;

            let result = personalize::personalize_fob(
                &mut sam_transport,
                &mut picc_transport,
                uid,
                &cfg,
                token_id,
                challenge,
            )
            .map_err(fmt_err)?;
            println!(
                "[personalize]  - personalized OK, token_id={}",
                result.token_id_hex
            );
            Ok(result)
        })();

        match outcome {
            Ok(result) => {
                println!("[personalize]  - fob {i}/{count}: OK");
                succeeded.push(result);
            }
            Err(e) => {
                eprintln!("[personalize]  - fob {i}/{count}: FAILED: {e}");
                failed.push((i, e));
            }
        }
    }

    println!(
        "[personalize] summary: {}/{count} fob(s) personalized successfully.",
        succeeded.len()
    );
    if !failed.is_empty() {
        eprintln!(
            "[personalize] failed fobs (NOT registered with the backend — see above for why):"
        );
        for (i, e) in &failed {
            eprintln!("[personalize]   - fob {i}: {e}");
        }
    }

    if succeeded.is_empty() {
        return Err(
            "every fob failed — see per-fob errors above; nothing to register with the backend"
                .to_string(),
        );
    }

    let token_ids = collect_token_ids(&succeeded);
    if admin {
        println!(
            "[personalize] registering {} ADMIN token-id(s) with the backend (never pooled as \
             employee-badge candidates)...",
            token_ids.len()
        );
        let reg = backend
            .register_admin_cards(&token_ids)
            .map_err(|e| format!("register-admin-cards: {e}"))?;
        println!(
            "[personalize] backend: added={} already_admin={} admin_count={}",
            reg.added, reg.already_admin, reg.admin_count
        );
    } else {
        println!(
            "[personalize] registering {} token-id(s) with the backend...",
            token_ids.len()
        );
        let uids = collect_uids(&succeeded);
        let reg = backend
            .register_to_pool(&token_ids, &uids)
            .map_err(|e| format!("register-to-pool: {e}"))?;
        println!(
            "[personalize] backend: pooled={} skipped_existing={} skipped_assigned={} \
             backfilled={} pool_size={}",
            reg.pooled, reg.skipped_existing, reg.skipped_assigned, reg.backfilled, reg.pool_size
        );
    }

    Ok(())
}

/// Block (polling every 500 ms) until a card is present on `reader_name`,
/// then read its UID via the PC/SC pseudo-APDU `FF CA 00 00 00` (Get Data —
/// UID) — the same command `reader/pcsc_reader.rs` uses for the interactive
/// kiosk's badge taps. Returns an error if the UID is not exactly 7 bytes
/// (the DESFire EV2 UID length this project's diversification scheme
/// assumes — see `crypto::diversification_input`).
#[cfg(feature = "real-reader")]
fn wait_for_card_and_read_uid(
    ctx: &pcsc::Context,
    reader_name: &std::ffi::CString,
) -> Result<[u8; 7], String> {
    use pcsc::{Protocols, ShareMode, MAX_BUFFER_SIZE};

    loop {
        match ctx.connect(reader_name, ShareMode::Shared, Protocols::ANY) {
            Ok(card) => {
                const GET_UID: [u8; 5] = [0xFF, 0xCA, 0x00, 0x00, 0x00];
                let mut buf = [0u8; MAX_BUFFER_SIZE];
                let resp = card
                    .transmit(&GET_UID, &mut buf)
                    .map_err(|e| format!("reading UID: {e}"))?;
                let uid_bytes = if resp.len() >= 2 {
                    &resp[..resp.len() - 2]
                } else {
                    resp
                };
                if uid_bytes.len() != 7 {
                    return Err(format!(
                        "expected a 7-byte DESFire UID, got {} bytes ({}) — is this a blank \
                         DESFire EV2 card in the PICC slot?",
                        uid_bytes.len(),
                        hex::encode(uid_bytes)
                    ));
                }
                let mut uid = [0u8; 7];
                uid.copy_from_slice(uid_bytes);
                return Ok(uid);
            }
            Err(pcsc::Error::NoSmartcard) => {
                std::thread::sleep(std::time::Duration::from_millis(500));
            }
            Err(e) => return Err(format!("connecting to PICC reader {reader_name:?}: {e}")),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_aid_hex_accepts_exactly_3_bytes() {
        assert_eq!(parse_aid_hex("010203").unwrap(), [0x01, 0x02, 0x03]);
        assert_eq!(parse_aid_hex("AABBCC").unwrap(), [0xAA, 0xBB, 0xCC]);
    }

    #[test]
    fn parse_aid_hex_rejects_wrong_length_and_bad_hex() {
        assert!(parse_aid_hex("0102").is_err(), "too short");
        assert!(parse_aid_hex("0102030405").is_err(), "too long");
        assert!(parse_aid_hex("zzzzzz").is_err(), "not hex");
    }

    #[test]
    fn parsed_args_parses_flag_value_pairs() {
        let args: Vec<String> = vec![
            "--count".into(),
            "5".into(),
            "--aid".into(),
            "010203".into(),
        ];
        let parsed = ParsedArgs::parse(&args).unwrap();
        assert_eq!(parsed.one("count"), Some("5"));
        assert_eq!(parsed.one("aid"), Some("010203"));
        assert_eq!(parsed.require_usize("count").unwrap(), 5);
    }

    #[test]
    fn parsed_args_rejects_missing_required_flag() {
        let args: Vec<String> = vec![];
        let parsed = ParsedArgs::parse(&args).unwrap();
        assert!(parsed.require_one("sam").is_err());
    }

    #[test]
    fn parsed_args_rejects_dangling_flag_without_value() {
        let args: Vec<String> = vec!["--count".into()];
        assert!(ParsedArgs::parse(&args).is_err());
    }

    #[test]
    fn parsed_args_admin_is_a_presence_only_switch() {
        let args: Vec<String> = vec![
            "--count".into(),
            "1".into(),
            "--admin".into(),
            "--aid".into(),
            "010203".into(),
        ];
        let parsed = ParsedArgs::parse(&args).unwrap();
        assert!(parsed.flag("admin"));
        // --admin consumed no value; --aid must still parse correctly.
        assert_eq!(parsed.one("aid"), Some("010203"));
    }

    #[test]
    fn parsed_args_admin_defaults_to_false_when_absent() {
        let args: Vec<String> = vec!["--count".into(), "1".into()];
        let parsed = ParsedArgs::parse(&args).unwrap();
        assert!(!parsed.flag("admin"));
    }

    #[test]
    fn collect_token_ids_preserves_order_and_count_across_a_batch() {
        let results: Vec<PersonalizedFob> = (0..5)
            .map(|i| PersonalizedFob {
                token_id_hex: format!("token-{i}"),
                uid_hex: format!("uid-{i}"),
            })
            .collect();
        let ids = collect_token_ids(&results);
        assert_eq!(ids.len(), 5);
        assert_eq!(
            ids,
            vec!["token-0", "token-1", "token-2", "token-3", "token-4"]
        );
    }

    #[test]
    fn collect_token_ids_on_empty_batch_is_empty() {
        assert_eq!(collect_token_ids(&[]), Vec::<String>::new());
    }

    #[test]
    fn collect_uids_preserves_order_and_count_across_a_batch() {
        let results: Vec<PersonalizedFob> = (0..5)
            .map(|i| PersonalizedFob {
                token_id_hex: format!("token-{i}"),
                uid_hex: format!("uid-{i}"),
            })
            .collect();
        let uids = collect_uids(&results);
        assert_eq!(
            uids,
            vec![
                Some("uid-0".to_owned()),
                Some("uid-1".to_owned()),
                Some("uid-2".to_owned()),
                Some("uid-3".to_owned()),
                Some("uid-4".to_owned()),
            ]
        );
    }

    #[test]
    fn collect_uids_on_empty_batch_is_empty() {
        assert_eq!(collect_uids(&[]), Vec::<Option<String>>::new());
    }

    #[test]
    fn default_personalize_config_carries_the_given_aid_and_key_numbers() {
        let cfg = default_personalize_config(
            [0x0A, 0x0B, 0x0C],
            0x02,
            0x01,
            FobKeys {
                company_key_entry_no: 0x04,
                company_key_entry_ver: 0x01,
            },
        );
        assert_eq!(cfg.aid, [0x0A, 0x0B, 0x0C]);
        assert_eq!(cfg.app_key_no, 0x02);
        assert_eq!(cfg.file_no, 0x01);
        assert_eq!(cfg.keys.company_key_entry_no, 0x04);
        assert_eq!(cfg.token_id_len, 16);
    }

    #[test]
    fn self_test_pipeline_runs_end_to_end_against_mocks() {
        // Exercises the exact same code path as `cmd_self_test`, without
        // going through stdout — a regression here means the `self-test`
        // subcommand itself would fail.
        let uid = [0x04, 0x11, 0x22, 0x33, 0x44, 0x55, 0x66];
        let cfg = default_personalize_config(
            [0x01, 0x02, 0x03],
            0x00,
            0x00,
            FobKeys {
                company_key_entry_no: 0x02,
                company_key_entry_ver: 0x00,
            },
        );
        let token_id = personalize::generate_token_id().unwrap();
        let challenge = personalize::PersonalizeChallenge {
            host_rnd1: [0x01u8; 12],
            host_rnd_a: [0x02u8; 16],
            ev2_rnd_a: [0x03u8; 16],
        ev2_rnd_a2: [0x04u8; 16],
        };
        let diversified_key = [0x5Au8; 16];
        let (mut sam, mut picc) = self_test_mocks(&challenge, diversified_key);
        let result =
            personalize::personalize_fob(&mut sam, &mut picc, uid, &cfg, token_id, challenge)
                .unwrap();
        assert_eq!(result.uid_hex, hex::encode(uid));
    }

    #[cfg(not(feature = "real-reader"))]
    #[test]
    fn cmd_run_without_real_reader_feature_errors_clearly() {
        let err = cmd_run(&[]).unwrap_err();
        assert!(err.contains("real-reader"));
    }
}
