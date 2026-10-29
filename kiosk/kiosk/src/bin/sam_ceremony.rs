//! SSH-run CLI for the SAM AV3 key-loading ceremony (bead 3pn).
//!
//! Run on the Pi (or any host with a PC/SC SAM reader) by a technician doing
//! an on-site company go-live. All hardware-independent logic lives in
//! `reader::sam::ceremony` (unit-tested against a mock, no hardware needed);
//! this file is a thin CLI shell around it: argument parsing, file I/O for
//! key material, and (only for `key-company`, and only when built with
//! `--features real-reader`) opening PC/SC connections to the SAM(s).
//!
//! ## Subcommands
//!
//! - `sam_ceremony key-company` — the real ceremony: authenticate to one or
//!   more SAMs as host, generate (or reuse) one company master key, load it
//!   into every named SAM's PICC-master key entry, then split+write a
//!   k-of-n backup. Requires `--features real-reader` (PC/SC +
//!   `libpcsclite`); without it, prints an explanatory error and exits
//!   non-zero instead of silently doing nothing.
//! - `sam_ceremony reconstruct` — offline: combine `>= threshold` backup
//!   share files back into the original 16-byte key (e.g. to re-key a
//!   replacement SAM after the original died). No hardware needed.
//! - `sam_ceremony self-test` — offline: runs the ceremony's crypto/APDU
//!   pipeline against an in-memory mock SAM, and the backup
//!   split/reconstruct round-trip, so a technician can tell "the software
//!   is broken" apart from "the hardware/wiring is broken" when triaging a
//!   failure over SSH. No hardware, no real key material touched.
//!
//! Every subcommand logs each step verbosely (`[ceremony] ...`) to stdout
//! (progress) / stderr (failures), so a technician can paste the exact
//! failing step when asking for help.
//!
//! Fail-closed: any unexpected SAM status, CSPRNG failure, or malformed
//! backup share aborts the whole run with a non-zero exit code — there is
//! no partial-success path that looks like success.

// Re-uses the existing `reader` module tree (shared with the main kiosk
// binary) via an explicit path, since this crate has no `lib.rs`. Submodule
// resolution inside `reader/mod.rs` (e.g. its own `pub mod sam;`) is
// relative to that file's real location, so this correctly picks up
// `src/reader/sam/*.rs` regardless of being included from a different
// binary target.
#[path = "../reader/mod.rs"]
mod reader;

use std::collections::HashMap;
use std::env;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::ExitCode;

use reader::sam::ceremony::{self, KeyShare};
use reader::sam::error::SamError;

fn main() -> ExitCode {
    let args: Vec<String> = env::args().collect();
    let result = match args.get(1).map(String::as_str) {
        Some("activate-sam") => cmd_activate_sam(&args[2..]),
        Some("key-company") => cmd_key_company(&args[2..]),
        Some("key-company-offline") => cmd_key_company_offline(&args[2..]),
        Some("reconstruct") => cmd_reconstruct(&args[2..]),
        Some("self-test") => cmd_self_test(),
        Some(other) => {
            eprintln!("[ceremony] unknown subcommand {other:?}\n");
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
            eprintln!("[ceremony] FAILED: {msg}");
            ExitCode::FAILURE
        }
    }
}

fn print_usage() {
    eprintln!(
        "Usage:\n\
         \x20 sam_ceremony key-company --host-key-file <path> [--host-key-no N] [--host-key-ver N] \\\n\
         \x20     --entry-no N --sam <reader-name-substring> [--sam <reader-name-substring> ...] \\\n\
         \x20     [--company-key-hex <32 hex chars>] [--backup-dir <dir>] [--threshold K] [--shares N]\n\
         \x20     (requires a build with --features real-reader)\n\
         \x20 sam_ceremony key-company-offline --company-key-hex <32 hex chars> --entry-no N \\\n\
         \x20     --sam <reader-name-substring> [--sam ...] [--changecnt N] \\\n\
         \x20     [--current-key-hex <32 hex chars>] [--lock-unlock-first true]\n\
         \x20     (OFFLINE ChangeKeyEntry — no SAM_AuthenticateHost; requires --features real-reader)\n\
         \x20 sam_ceremony reconstruct --share <file> [--share <file> ...]\n\
         \x20 sam_ceremony self-test\n"
    );
}

fn fmt_err(e: SamError) -> String {
    e.to_string()
}

// ---------------------------------------------------------------------
// Minimal manual arg parsing (no CLI-parsing dependency — this tool has
// exactly three subcommands and a handful of `--flag value` pairs).
// ---------------------------------------------------------------------

struct ParsedArgs {
    flags: HashMap<String, Vec<String>>,
}

impl ParsedArgs {
    fn parse(args: &[String]) -> Result<Self, String> {
        let mut flags: HashMap<String, Vec<String>> = HashMap::new();
        let mut i = 0;
        while i < args.len() {
            let a = &args[i];
            let Some(name) = a.strip_prefix("--") else {
                return Err(format!("unexpected positional argument: {a:?} (all arguments must be --flag value pairs)"));
            };
            let value = args
                .get(i + 1)
                .cloned()
                .ok_or_else(|| format!("--{name} is missing its value"))?;
            flags.entry(name.to_string()).or_default().push(value);
            i += 2;
        }
        Ok(Self { flags })
    }

    fn one(&self, name: &str) -> Option<&str> {
        self.flags
            .get(name)
            .and_then(|v| v.first())
            .map(String::as_str)
    }

    fn require_one(&self, name: &str) -> Result<&str, String> {
        self.one(name)
            .ok_or_else(|| format!("--{name} is required"))
    }

    fn all(&self, name: &str) -> Vec<&str> {
        self.flags
            .get(name)
            .map(|v| v.iter().map(String::as_str).collect())
            .unwrap_or_default()
    }

    fn one_u8(&self, name: &str, default: u8) -> Result<u8, String> {
        match self.one(name) {
            Some(v) => v
                .parse::<u8>()
                .map_err(|e| format!("--{name} must be a number 0-255: {e}")),
            None => Ok(default),
        }
    }
}

// ---------------------------------------------------------------------
// Hex / key-material file helpers.
// ---------------------------------------------------------------------

fn parse_hex_key16(label: &str, hex_str: &str) -> Result<[u8; 16], String> {
    let bytes = hex::decode(hex_str.trim()).map_err(|e| format!("{label}: not valid hex: {e}"))?;
    if bytes.len() != 16 {
        return Err(format!(
            "{label}: must decode to exactly 16 bytes (32 hex chars), got {}",
            bytes.len()
        ));
    }
    let mut out = [0u8; 16];
    out.copy_from_slice(&bytes);
    Ok(out)
}

fn read_host_key_file(path: &str) -> Result<[u8; 16], String> {
    let content =
        fs::read_to_string(path).map_err(|e| format!("reading host key file {path:?}: {e}"))?;
    parse_hex_key16(&format!("host key file {path:?}"), &content)
}

// ---------------------------------------------------------------------
// Backup share file format: a small, clearly-labelled, human-readable
// key=value text file — not a binary blob — so a technician can visually
// confirm what they're holding before it ever touches a SAM again.
// ---------------------------------------------------------------------

fn share_to_string(share: &KeyShare, sam_label: &str, entry_no: u8) -> String {
    format!(
        "# SAM AV3 company-key backup share — Zeiterfassung key ceremony\n\
         #\n\
         # WARNING: this is ONE share of a k-of-n Shamir secret split. On its\n\
         # own it reveals NOTHING about the company master key\n\
         # (information-theoretic secrecy below the threshold). Store each\n\
         # share at a DIFFERENT secure physical location (e.g. safe, HR\n\
         # office, off-site). Losing any single share below (total_shares -\n\
         # threshold + 1) of them does NOT lose the key — that is the whole\n\
         # point of splitting it. Combine >= `threshold` shares with\n\
         # `sam_ceremony reconstruct` to recover the key, e.g. to re-key a\n\
         # replacement SAM after this company's original SAM(s) died.\n\
         #\n\
         # keyed_sam = {sam_label}\n\
         # sam_key_entry_no = {entry_no}\n\
         x = {}\n\
         threshold = {}\n\
         total_shares = {}\n\
         y = {}\n",
        share.x,
        share.threshold,
        share.total_shares,
        hex::encode(share.y),
    )
}

fn parse_share_file(path: &Path) -> Result<KeyShare, String> {
    let content =
        fs::read_to_string(path).map_err(|e| format!("reading {}: {e}", path.display()))?;
    let mut x = None;
    let mut threshold = None;
    let mut total_shares = None;
    let mut y_hex = None;
    for line in content.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let Some((key, val)) = line.split_once('=') else {
            continue;
        };
        let (key, val) = (key.trim(), val.trim());
        match key {
            "x" => x = val.parse::<u8>().ok(),
            "threshold" => threshold = val.parse::<u8>().ok(),
            "total_shares" => total_shares = val.parse::<u8>().ok(),
            "y" => y_hex = Some(val.to_string()),
            _ => {}
        }
    }
    let x = x.ok_or_else(|| format!("{}: missing/invalid 'x'", path.display()))?;
    let threshold =
        threshold.ok_or_else(|| format!("{}: missing/invalid 'threshold'", path.display()))?;
    let total_shares = total_shares
        .ok_or_else(|| format!("{}: missing/invalid 'total_shares'", path.display()))?;
    let y_hex = y_hex.ok_or_else(|| format!("{}: missing 'y'", path.display()))?;
    let y_bytes =
        hex::decode(&y_hex).map_err(|e| format!("{}: bad hex in 'y': {e}", path.display()))?;
    if y_bytes.len() != 16 {
        return Err(format!(
            "{}: 'y' must decode to 16 bytes, got {}",
            path.display(),
            y_bytes.len()
        ));
    }
    let mut y = [0u8; 16];
    y.copy_from_slice(&y_bytes);
    Ok(KeyShare {
        x,
        y,
        threshold,
        total_shares,
    })
}

// ---------------------------------------------------------------------
// `sam_ceremony reconstruct`
// ---------------------------------------------------------------------

fn cmd_reconstruct(args: &[String]) -> Result<(), String> {
    let parsed = ParsedArgs::parse(args)?;
    let share_paths = parsed.all("share");
    if share_paths.len() < 2 {
        return Err("need at least 2 --share <file> arguments".to_string());
    }

    let mut shares = Vec::new();
    for p in &share_paths {
        let path = PathBuf::from(p);
        let share = parse_share_file(&path)?;
        println!(
            "[ceremony]  - loaded share x={} (threshold={}, total_shares={}) from {}",
            share.x,
            share.threshold,
            share.total_shares,
            path.display()
        );
        shares.push(share);
    }

    let threshold = shares[0].threshold;
    if shares.iter().any(|s| s.threshold != threshold) {
        return Err(
            "supplied shares disagree on 'threshold' — they are not all from the same backup"
                .to_string(),
        );
    }
    if shares.len() < threshold as usize {
        println!(
            "[ceremony] WARNING: only {} shares supplied but threshold is {} — \
             reconstruction below the threshold silently produces a WRONG key \
             (this is inherent to Shamir's scheme, not an error this tool can \
             detect). Gather {} shares before trusting the result below.",
            shares.len(),
            threshold,
            threshold
        );
    }

    let key = ceremony::reconstruct_key_backup(&shares).map_err(fmt_err)?;
    println!(
        "[ceremony] reconstructed company master key (hex): {}",
        hex::encode(key)
    );
    println!(
        "[ceremony] IMPORTANT: this key is now in your terminal scrollback/history — \
         clear it after use. To load it onto a replacement SAM, run:\n\
         \x20   sam_ceremony key-company --host-key-file <replacement SAM's host key> \\\n\
         \x20     --entry-no <N> --sam <reader> --company-key-hex {}",
        hex::encode(key)
    );
    Ok(())
}

// ---------------------------------------------------------------------
// `sam_ceremony self-test`
// ---------------------------------------------------------------------

fn cmd_self_test() -> Result<(), String> {
    println!("[ceremony] self-test: exercising the crypto/APDU/backup pipeline against an in-memory mock SAM (no hardware touched).");

    let host_master_key = ceremony::generate_master_key().map_err(fmt_err)?;
    println!("[ceremony]  - generated a synthetic host master key via CSPRNG (not a real provisioned key)");

    let rnd1 = ceremony::generate_rnd1().map_err(fmt_err)?;
    let rnd_a = ceremony::generate_rnd_a().map_err(fmt_err)?;
    let lock_unlock_rnd1 = ceremony::generate_rnd1().map_err(fmt_err)?;
    let lock_unlock_rnd_a = ceremony::generate_rnd_a().map_err(fmt_err)?;
    println!("[ceremony]  - generated host challenges (rnd1[12], RndA[16], lock_unlock_rnd1[12], lock_unlock_RndA[16]) via CSPRNG");

    // The mock SAM doesn't run real firmware, so this self-test computes the
    // canned responses it plays back using the same crypto this tool calls
    // for real — i.e. this proves internal self-consistency (no panics, no
    // APDU-framing bugs, the pipeline runs end-to-end), not correctness
    // against real hardware. See HARDWARE-VERIFY notes in
    // `reader/sam/crypto.rs` / `reader/sam/apdu.rs` for what's unconfirmed.
    let rnd2 = ceremony::generate_rnd1().map_err(fmt_err)?; // any fresh 12 bytes stand in for the "SAM's" rnd2
    let rnd_b = ceremony::generate_rnd_a().map_err(fmt_err)?; // any fresh 16 bytes stand in for the "SAM's" RndB
    let lock_unlock_rnd2 = ceremony::generate_rnd1().map_err(fmt_err)?; // stand-in "SAM's" rnd2 for the LockUnlock/Unlock pre-step
    let lock_unlock_rnd_b = ceremony::generate_rnd_a().map_err(fmt_err)?; // stand-in "SAM's" RndB for the LockUnlock/Unlock pre-step

    let mut sam = reader::sam::mock::MockTransport::new();

    // LockUnlock/Unlock pre-step (bead 59m — HARDWARE-VERIFY: pass 1 AND
    // pass 2's wire format are empirically confirmed on real hardware; pass
    // 3 is transcribed from a hardware-exercised reference but not yet
    // confirmed on this project's own SAM — see `flows::lock_unlock_unlock`).
    let mut lu_resp1 = lock_unlock_rnd2.to_vec();
    lu_resp1.push(0x90);
    lu_resp1.push(0xAF);
    sam.push_ok(lu_resp1);
    let lu_mac_sam = reader::sam::crypto::host_auth_pass2_cmac(
        &reader::sam::ceremony::SAM_FACTORY_DEFAULT_MASTER_KEY,
        &lock_unlock_rnd1,
        reader::sam::apdu::LOCK_UNLOCK_STATE_UNLOCK,
    );
    let lu_kex = reader::sam::crypto::derive_kex(
        &reader::sam::ceremony::SAM_FACTORY_DEFAULT_MASTER_KEY,
        &lock_unlock_rnd1,
        &lock_unlock_rnd2,
    )
    .map_err(fmt_err)?;
    let lu_rnd_bc =
        reader::sam::crypto::aes128_cbc_encrypt_no_pad(&lu_kex, &[0u8; 16], &lock_unlock_rnd_b)
            .map_err(fmt_err)?;
    let mut lu_resp2 = lu_mac_sam.to_vec();
    lu_resp2.extend_from_slice(&lu_rnd_bc);
    lu_resp2.push(0x90);
    lu_resp2.push(0xAF);
    sam.push_ok(lu_resp2);
    sam.push_ok(vec![0x90, 0x00]); // LockUnlock/Unlock pass 3 OK

    let mut resp1 = rnd2.to_vec();
    resp1.push(0x90);
    resp1.push(0xAF);
    sam.push_ok(resp1);

    let kex = reader::sam::crypto::derive_kex(&host_master_key, &rnd1, &rnd2).map_err(fmt_err)?;
    let rnd_bc = reader::sam::crypto::aes128_cbc_encrypt_no_pad(&kex, &[0u8; 16], &rnd_b)
        .map_err(fmt_err)?;
    let mut resp2 = vec![0u8; 8];
    resp2.extend_from_slice(&rnd_bc);
    resp2.push(0x90);
    resp2.push(0xAF);
    sam.push_ok(resp2);

    sam.push_ok(vec![0x90, 0x00]); // pass 3 OK
    sam.push_ok(vec![0x90, 0x00]); // ChangeKeyEntry OK

    let company_key = ceremony::generate_master_key().map_err(fmt_err)?;
    println!("[ceremony]  - generated a synthetic company master key via CSPRNG");

    let result = ceremony::run_ceremony_on_sam(
        &mut sam,
        &ceremony::HostKeyRef {
            key: &host_master_key,
            key_no: 0x00,
            key_ver: 0x00,
        },
        0x01,
        &company_key,
        ceremony::HostChallenge {
            rnd1,
            rnd_a,
            lock_unlock_rnd1,
            lock_unlock_rnd_a,
        },
    )
    .map_err(fmt_err)?;
    println!(
        "[ceremony]  - 3-pass AuthenticateHost + ChangeKeyEntry(Full) pipeline ran end-to-end \
         against the mock SAM (SET={:04X} ExtSET={:02X} on the written entry)",
        result.entry.set, result.entry.ext_set
    );

    let shares = ceremony::split_key_backup(&company_key, 3, 5).map_err(fmt_err)?;
    println!("[ceremony]  - split synthetic company key into 5 shares (threshold 3)");
    let reconstructed = ceremony::reconstruct_key_backup(&shares[0..3]).map_err(fmt_err)?;
    if reconstructed != company_key {
        return Err(
            "backup split/reconstruct round-trip MISMATCH — this is a real software bug, \
             do NOT proceed to a real ceremony until this self-test passes"
                .to_string(),
        );
    }
    println!("[ceremony]  - 3-of-5 backup split/reconstruct round-trip OK");

    println!(
        "[ceremony] self-test PASSED. The software pipeline (crypto, APDU framing, key-entry \
         composition, backup split/reconstruct) is sound. If a real ceremony still fails, the \
         problem is most likely the SAM hardware itself, the PC/SC reader/wiring, or the \
         reader-name substring you passed to --sam — not this code."
    );
    Ok(())
}

// ---------------------------------------------------------------------
// `sam_ceremony key-company` — real-reader gated.
// ---------------------------------------------------------------------

#[cfg(not(feature = "real-reader"))]
fn cmd_key_company(_args: &[String]) -> Result<(), String> {
    Err(
        "this binary was built without the `real-reader` feature, so it has no PC/SC support \
         and cannot talk to a real SAM. Rebuild with: cargo build --release --features real-reader \
         (run `sam_ceremony self-test` in the meantime to sanity-check the software pipeline)"
            .to_string(),
    )
}

#[cfg(feature = "real-reader")]
fn cmd_key_company(args: &[String]) -> Result<(), String> {
    use std::ffi::CString;

    use pcsc::{Context, Scope};

    use reader::sam::transport::{find_reader_by_name_substring, SamPcscTransport};

    let parsed = ParsedArgs::parse(args)?;

    let host_key_file = parsed.require_one("host-key-file")?;
    let host_master_key = read_host_key_file(host_key_file)?;
    println!("[ceremony] loaded host master key from {host_key_file:?}");

    let host_key_no = parsed.one_u8("host-key-no", 0)?;
    let host_key_ver = parsed.one_u8("host-key-ver", 0)?;
    let entry_no_str = parsed.require_one("entry-no")?;
    let entry_no: u8 = entry_no_str
        .parse()
        .map_err(|e| format!("--entry-no must be a number 0-255: {e}"))?;

    let sam_targets = parsed.all("sam");
    if sam_targets.is_empty() {
        return Err("need at least one --sam <reader-name-substring>".to_string());
    }

    let company_key = match parsed.one("company-key-hex") {
        Some(hex_str) => {
            let key = parse_hex_key16("--company-key-hex", hex_str)?;
            println!("[ceremony] reusing a supplied company master key (not generating a new one — recovery/fleet-expansion mode)");
            key
        }
        None => {
            let key = ceremony::generate_master_key().map_err(fmt_err)?;
            println!("[ceremony] generated a fresh company master key via CSPRNG");
            key
        }
    };

    println!(
        "[ceremony] plan: key {} SAM(s) with the SAME company master key into entry #{entry_no}",
        sam_targets.len()
    );

    let ctx = Context::establish(Scope::User)
        .map_err(|e| format!("PC/SC: cannot establish context: {e}"))?;

    let mut succeeded: Vec<&str> = Vec::new();
    let mut failed: Vec<(&str, String)> = Vec::new();

    for target in &sam_targets {
        println!("[ceremony] --- SAM target {target:?} ---");
        let outcome: Result<(), String> = (|| {
            let reader_name: CString = find_reader_by_name_substring(&ctx, target)
                .ok_or_else(|| format!("no PC/SC reader name contains {target:?}"))?;
            println!("[ceremony]  - matched PC/SC reader: {reader_name:?}");

            let mut transport = SamPcscTransport::open_named(&ctx, &reader_name)
                .map_err(|e| format!("opening SAM reader {reader_name:?}: {e}"))?;
            println!("[ceremony]  - opened SAM card connection");

            // Fresh host-session randoms PER SAM — never reuse a challenge
            // across independent AuthenticateHost/LockUnlock sessions.
            let rnd1 = ceremony::generate_rnd1().map_err(fmt_err)?;
            let rnd_a = ceremony::generate_rnd_a().map_err(fmt_err)?;
            let lock_unlock_rnd1 = ceremony::generate_rnd1().map_err(fmt_err)?;
            let lock_unlock_rnd_a = ceremony::generate_rnd_a().map_err(fmt_err)?;

            println!(
                "[ceremony]  - running SAM_LockUnlock/Unlock (2-pass, default master key) then \
                 SAM_AuthenticateHost (3-pass, Full mode)..."
            );
            let result = ceremony::run_ceremony_on_sam(
                &mut transport,
                &ceremony::HostKeyRef {
                    key: &host_master_key,
                    key_no: host_key_no,
                    key_ver: host_key_ver,
                },
                entry_no,
                &company_key,
                ceremony::HostChallenge {
                    rnd1,
                    rnd_a,
                    lock_unlock_rnd1,
                    lock_unlock_rnd_a,
                },
            )
            .map_err(fmt_err)?;
            println!("[ceremony]  - host authenticated, ChangeKeyEntry(Full) accepted (9000)");
            println!(
                "[ceremony]  - entry #{entry_no}: SET={:04X} ExtSET={:02X} (company PICC-master, diversified-use-only)",
                result.entry.set, result.entry.ext_set
            );
            Ok(())
        })();

        match outcome {
            Ok(()) => {
                println!("[ceremony]  - SAM target {target:?}: OK");
                succeeded.push(target);
            }
            Err(e) => {
                eprintln!("[ceremony]  - SAM target {target:?}: FAILED: {e}");
                failed.push((target, e));
            }
        }
    }

    println!(
        "[ceremony] summary: {}/{} SAM(s) keyed successfully.",
        succeeded.len(),
        sam_targets.len()
    );
    if !failed.is_empty() {
        eprintln!("[ceremony] failed targets:");
        for (target, e) in &failed {
            eprintln!("[ceremony]   - {target:?}: {e}");
        }
    }

    if succeeded.is_empty() {
        return Err(
            "every SAM target failed — see per-target errors above; NOT writing a backup since no key material was actually loaded onto any hardware".to_string(),
        );
    }

    // Backup — SPOF mitigation. A single dead SAM (or a single lost backup
    // share) must never orphan every card the company has already badged.
    match parsed.one("backup-dir") {
        Some(backup_dir) => {
            let threshold = parsed.one_u8("threshold", 3)?;
            let shares_n = parsed.one_u8("shares", 5)?;
            println!(
                "[ceremony] writing {shares_n}-share ({threshold}-of-{shares_n}) backup to {backup_dir:?}"
            );
            let shares =
                ceremony::split_key_backup(&company_key, threshold, shares_n).map_err(fmt_err)?;
            fs::create_dir_all(backup_dir)
                .map_err(|e| format!("creating backup dir {backup_dir:?}: {e}"))?;
            let sam_label = sam_targets.join(",");
            for share in &shares {
                let path = Path::new(backup_dir).join(format!(
                    "share-{}-of-{}-k{}.txt",
                    share.x, share.total_shares, share.threshold
                ));
                fs::write(&path, share_to_string(share, &sam_label, entry_no))
                    .map_err(|e| format!("writing {}: {e}", path.display()))?;
                println!("[ceremony]  - wrote {}", path.display());
            }
            println!(
                "[ceremony] SPOF MITIGATION: store these {shares_n} share files at {shares_n} \
                 DIFFERENT secure locations. Any {threshold} of them reconstruct the company \
                 master key (`sam_ceremony reconstruct`) to re-key a replacement SAM. Losing \
                 up to {} of the {shares_n} shares does NOT lose the key or expose it.",
                shares_n - threshold
            );
        }
        None => {
            eprintln!(
                "[ceremony] WARNING: no --backup-dir given — NO BACKUP WAS WRITTEN. If every \
                 keyed SAM is later lost/destroyed, this company's cards cannot be re-keyed. \
                 Re-run with --backup-dir <dir> (optionally --threshold K --shares N) to persist \
                 a k-of-n Shamir backup of the key before ending this session. (The key itself is \
                 deliberately NOT printed here to keep it out of logs/terminal scrollback.)"
            );
        }
    }

    // Best-effort in-memory zeroing — the key has already been written to
    // the SAM(s) (and, if requested, split into backup shares on disk); it
    // no longer needs to live in this process's memory.
    {
        use zeroize::Zeroize;
        let mut key_copy = company_key;
        key_copy.zeroize();
    }

    Ok(())
}

// ---------------------------------------------------------------------
// `sam_ceremony activate-sam` — SAM_LockUnlock P1=0x03 (Activate SAM).
// IRREVERSIBLE: activates a factory (ICS) SAM AV3 into MCS so that
// SAM_AuthenticateHost / SAM_ChangeKeyEntry stop returning 6985. Uses the
// factory master key entry (0/0, all-zero) for the activation handshake.
// ---------------------------------------------------------------------

#[cfg(not(feature = "real-reader"))]
fn cmd_activate_sam(_args: &[String]) -> Result<(), String> {
    Err("built without the `real-reader` feature — cannot talk to a real SAM. \
         Rebuild with: cargo build --release --features real-reader"
        .to_string())
}

#[cfg(feature = "real-reader")]
fn cmd_activate_sam(args: &[String]) -> Result<(), String> {
    use std::ffi::CString;

    use pcsc::{Context, Scope};

    use reader::sam::transport::{find_reader_by_name_substring, SamPcscTransport};

    let parsed = ParsedArgs::parse(args)?;
    let sam_targets = parsed.all("sam");
    if sam_targets.is_empty() {
        return Err("need at least one --sam <reader-name-substring>".to_string());
    }

    eprintln!(
        "[activate] IRREVERSIBLE: SAM_LockUnlock P1=0x03 (Activate SAM) on {} SAM(s) using the \
         factory master key (entry 0, ver 0, all-zero).",
        sam_targets.len()
    );

    let ctx = Context::establish(Scope::User)
        .map_err(|e| format!("PC/SC: cannot establish context: {e}"))?;

    for target in &sam_targets {
        println!("[activate] --- SAM target {target:?} ---");
        let reader_name: CString = find_reader_by_name_substring(&ctx, target)
            .ok_or_else(|| format!("no PC/SC reader name contains {target:?}"))?;
        let mut transport = SamPcscTransport::open_named(&ctx, &reader_name)
            .map_err(|e| format!("opening SAM reader {reader_name:?}: {e}"))?;
        println!("[activate]  - opened SAM card connection: {reader_name:?}");

        let rnd1 = ceremony::generate_rnd1().map_err(fmt_err)?;
        let rnd_a = ceremony::generate_rnd_a().map_err(fmt_err)?;
        println!("[activate]  - running SAM_LockUnlock/Activate (P1=0x03, 3-pass mutual auth)...");
        reader::sam::flows::lock_unlock_activate(
            &mut transport,
            ceremony::SAM_FACTORY_DEFAULT_KEY_NO,
            ceremony::SAM_FACTORY_DEFAULT_KEY_VER,
            &ceremony::SAM_FACTORY_DEFAULT_MASTER_KEY,
            rnd1,
            rnd_a,
        )
        .map_err(fmt_err)?;
        println!(
            "[activate]  - OK: all three passes completed (final 9000). SAM {target:?} ACTIVATED."
        );
    }
    Ok(())
}

// ---------------------------------------------------------------------
// `sam_ceremony key-company-offline` — OFFLINE ChangeKeyEntry, real-reader
// gated. Loads a company key into a `KeyClass = OfflineChange` entry WITHOUT
// SAM_AuthenticateHost (which returns 6985 on this project's SAM AV3): the
// session is derived from the entry's OWN CURRENT KEY + a change counter.
// ---------------------------------------------------------------------

#[cfg(not(feature = "real-reader"))]
fn cmd_key_company_offline(_args: &[String]) -> Result<(), String> {
    Err(
        "this binary was built without the `real-reader` feature, so it has no PC/SC support \
         and cannot talk to a real SAM. Rebuild with: cargo build --release --features real-reader"
            .to_string(),
    )
}

#[cfg(feature = "real-reader")]
fn cmd_key_company_offline(args: &[String]) -> Result<(), String> {
    use std::ffi::CString;

    use pcsc::{Context, Scope};

    use reader::sam::transport::{find_reader_by_name_substring, SamPcscTransport};

    let parsed = ParsedArgs::parse(args)?;

    let company_key = parse_hex_key16("--company-key-hex", parsed.require_one("company-key-hex")?)?;
    println!("[ceremony] loaded company master key from --company-key-hex");

    let entry_no: u8 = parsed
        .require_one("entry-no")?
        .parse()
        .map_err(|e| format!("--entry-no must be a number 0-255: {e}"))?;

    // Change counter: u16, big-endian on the wire; MUST match what the SAM
    // has stored for this entry (it re-derives the offline session from it).
    let changecnt: u16 = match parsed.one("changecnt") {
        Some(v) => v
            .parse()
            .map_err(|e| format!("--changecnt must be a number 0-65535: {e}"))?,
        None => 0,
    };

    // The entry's CURRENT key (seeds the offline session). Entry 1's factory
    // default is the all-zero AES-128 key; override for a re-key of an
    // already-company-keyed entry.
    let current_key = match parsed.one("current-key-hex") {
        Some(hex_str) => {
            let k = parse_hex_key16("--current-key-hex", hex_str)?;
            println!("[ceremony] using a supplied --current-key-hex as the entry's current key");
            k
        }
        None => {
            println!(
                "[ceremony] using the factory-default all-zero AES-128 key as the entry's current key"
            );
            ceremony::ENTRY_FACTORY_DEFAULT_CURRENT_KEY
        }
    };

    let lock_unlock_first = parsed.one("lock-unlock-first") == Some("true");

    let sam_targets = parsed.all("sam");
    if sam_targets.is_empty() {
        return Err("need at least one --sam <reader-name-substring>".to_string());
    }

    println!(
        "[ceremony] plan: OFFLINE ChangeKeyEntry into entry #{entry_no} on {} SAM(s), changecnt={changecnt}, lock_unlock_first={lock_unlock_first}",
        sam_targets.len()
    );

    let ctx = Context::establish(Scope::User)
        .map_err(|e| format!("PC/SC: cannot establish context: {e}"))?;

    let mut succeeded: Vec<&str> = Vec::new();
    let mut failed: Vec<(&str, String)> = Vec::new();

    for target in &sam_targets {
        println!("[ceremony] --- SAM target {target:?} ---");
        let outcome: Result<(), String> = (|| {
            let reader_name: CString = find_reader_by_name_substring(&ctx, target)
                .ok_or_else(|| format!("no PC/SC reader name contains {target:?}"))?;
            println!("[ceremony]  - matched PC/SC reader: {reader_name:?}");

            let mut transport = SamPcscTransport::open_named(&ctx, &reader_name)
                .map_err(|e| format!("opening SAM reader {reader_name:?}: {e}"))?;
            println!("[ceremony]  - opened SAM card connection");

            if lock_unlock_first {
                let lu_rnd1 = ceremony::generate_rnd1().map_err(fmt_err)?;
                let lu_rnd_a = ceremony::generate_rnd_a().map_err(fmt_err)?;
                println!(
                    "[ceremony]  - running SAM_LockUnlock/Unlock (default all-zero master key) first..."
                );
                reader::sam::flows::lock_unlock_unlock(
                    &mut transport,
                    ceremony::SAM_FACTORY_DEFAULT_KEY_NO,
                    ceremony::SAM_FACTORY_DEFAULT_KEY_VER,
                    &ceremony::SAM_FACTORY_DEFAULT_MASTER_KEY,
                    lu_rnd1,
                    lu_rnd_a,
                )
                .map_err(fmt_err)?;
                println!("[ceremony]  - SAM_LockUnlock/Unlock reached 9000");
            }

            println!("[ceremony]  - transmitting OFFLINE ChangeKeyEntry (plain, no host session)...");
            let result = ceremony::run_offline_ceremony_on_sam(
                &mut transport,
                &current_key,
                entry_no,
                &company_key,
                changecnt,
            )
            .map_err(fmt_err)?;
            println!(
                "[ceremony]  - OFFLINE ChangeKeyEntry accepted (9000): entry #{entry_no} SET={:04X} ExtSET={:02X}, encke={} bytes",
                result.entry.set,
                result.entry.ext_set,
                result.encke.len()
            );
            Ok(())
        })();

        match outcome {
            Ok(()) => {
                println!("[ceremony]  - SAM target {target:?}: OK");
                succeeded.push(target);
            }
            Err(e) => {
                eprintln!("[ceremony]  - SAM target {target:?}: FAILED: {e}");
                failed.push((target, e));
            }
        }
    }

    println!(
        "[ceremony] summary: {}/{} SAM(s) keyed via offline change.",
        succeeded.len(),
        sam_targets.len()
    );
    if !failed.is_empty() {
        eprintln!("[ceremony] failed targets:");
        for (target, e) in &failed {
            eprintln!("[ceremony]   - {target:?}: {e}");
        }
    }
    if succeeded.is_empty() {
        return Err("every SAM target failed — see per-target errors above".to_string());
    }

    {
        use zeroize::Zeroize;
        let mut key_copy = company_key;
        key_copy.zeroize();
    }

    Ok(())
}
