//! Startup configuration, loaded from environment variables.

use std::env;

use thiserror::Error;

/// Which card reader backend to drive.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReaderKind {
    /// Reads simulated badge taps from stdin. Needs no hardware.
    Mock,
    /// Real PC/SC reader (ACR1252U-M1). Only available with the `real-reader`
    /// feature compiled in.
    Pcsc,
}

/// Errors that can occur while assembling [`Config`] from the environment.
#[derive(Debug, Error)]
pub enum ConfigError {
    /// A required variable was not set.
    #[error("required environment variable {0} is not set")]
    Missing(&'static str),
    /// `KIOSK_READER` held an unrecognised value.
    #[error("invalid KIOSK_READER value {value:?} (expected \"mock\" or \"pcsc\")")]
    InvalidReader { value: String },
    /// A `KIOSK_SAM_*` numeric field wasn't a valid `u8` (0-255).
    #[error("environment variable {key} must be a number 0-255: {value:?}")]
    InvalidU8 { key: &'static str, value: String },
    /// `KIOSK_SAM_AID` wasn't exactly 6 hex chars (3 bytes).
    #[error("KIOSK_SAM_AID must be exactly 6 hex chars (3 bytes), got {value:?}: {reason}")]
    InvalidSamAid { value: String, reason: String },
}

/// Resolved kiosk configuration.
#[derive(Debug, Clone)]
pub struct Config {
    /// Backend base URL, e.g. `http://localhost:8000`.
    pub backend_url: String,
    /// Tenant UUID sent in the `X-Tenant-Id` header.
    pub tenant_id: String,
    /// Device API key sent in the `X-Device-Api-Key` header.
    pub device_key: String,
    /// Logical device identifier (from `KIOSK_DEVICE_ID`). The two-phase NFC
    /// endpoints don't take it in the body, but it's retained as part of the
    /// configuration contract.
    #[allow(dead_code)]
    pub device_id: String,
    /// Which reader backend to construct at startup.
    pub reader: ReaderKind,

    // -- SAM-mediated punch authentication (bead bi0) -----------------
    // Only consumed by `main::build_reader`'s `real-reader`-gated branch
    // (i.e. only when `reader == ReaderKind::Pcsc` on a `--features
    // real-reader` build); the default (mock) build never reads them,
    // hence `#[allow(dead_code)]` on every field below, matching
    // `device_id` above. Values must match what bead 4ty's
    // `sam_personalize` CLI used to personalize the fobs — same AID,
    // app-key-no, file-no, and SAM key-entry conventions (`--aid`,
    // `--app-key-no`, `--file-no`, `--company-key-entry-no/ver`).
    /// The application ID selected/authenticated on each fob (6 hex chars,
    /// 3 bytes), e.g. from `KIOSK_SAM_AID=010203`. Required (no silent
    /// default) when `KIOSK_READER=pcsc` — an unset/wrong AID would make
    /// every tap fail closed against real hardware, which is safe but a
    /// confusing "nothing works" symptom worth catching at startup instead.
    #[allow(dead_code)]
    pub sam_aid: [u8; 3],
    /// Which app key number holds the diversified key (`KIOSK_SAM_APP_KEY_NO`,
    /// default `0` — 4ty's single-key-app convention).
    #[allow(dead_code)]
    pub sam_app_key_no: u8,
    /// SAM key entry holding the company master key
    /// (`KIOSK_SAM_KEY_ENTRY_NO`). Required when `KIOSK_READER=pcsc`, same
    /// reasoning as `sam_aid`.
    #[allow(dead_code)]
    pub sam_key_entry_no: u8,
    /// SAM key entry version (`KIOSK_SAM_KEY_ENTRY_VER`, default `0`).
    #[allow(dead_code)]
    pub sam_key_entry_ver: u8,
    /// The `StdDataFile` file number holding the token-id
    /// (`KIOSK_SAM_FILE_NO`, default `0`).
    #[allow(dead_code)]
    pub sam_file_no: u8,
    /// Length of the token-id, in bytes (`KIOSK_SAM_TOKEN_ID_LEN`, default
    /// `16` — this project's 128-bit random token-id).
    #[allow(dead_code)]
    pub sam_token_id_len: u8,
    /// Case-insensitive substring identifying the SAM-slot PC/SC reader
    /// (`KIOSK_SAM_READER`, default `"SAM"`).
    #[allow(dead_code)]
    pub sam_reader_substr: String,
    /// Case-insensitive substring identifying the PICC-slot (contactless)
    /// PC/SC reader (`KIOSK_PICC_READER`, default `"PICC"`).
    #[allow(dead_code)]
    pub picc_reader_substr: String,
}

impl Config {
    /// Build a [`Config`] from process environment variables.
    ///
    /// Defaults:
    /// - `KIOSK_BACKEND_URL` → `http://localhost:8000`
    /// - `KIOSK_DEVICE_ID` → `TERMINAL-01`
    /// - `KIOSK_READER` → `mock`
    /// - `KIOSK_SAM_READER` → `SAM`, `KIOSK_PICC_READER` → `PICC`
    /// - `KIOSK_SAM_APP_KEY_NO` / `KIOSK_SAM_KEY_ENTRY_VER` /
    ///   `KIOSK_SAM_FILE_NO` → `0`, `KIOSK_SAM_TOKEN_ID_LEN` → `16`
    ///
    /// `KIOSK_TENANT_ID` and `KIOSK_DEVICE_KEY` are always required (no
    /// fallback — an unset device key would otherwise silently authenticate
    /// against the backend with a fixed, well-known value). `KIOSK_SAM_AID`
    /// and `KIOSK_SAM_KEY_ENTRY_NO` are required **only** when
    /// `KIOSK_READER=pcsc` — the mock (default) build never touches them.
    ///
    /// # Errors
    ///
    /// Returns [`ConfigError::Missing`] when a required variable is
    /// unset/empty, [`ConfigError::InvalidReader`] when `KIOSK_READER` is not
    /// recognised, [`ConfigError::InvalidU8`] when a `KIOSK_SAM_*` numeric
    /// field isn't a valid `u8`, or [`ConfigError::InvalidSamAid`] when
    /// `KIOSK_SAM_AID` isn't exactly 6 hex chars.
    pub fn from_env() -> Result<Self, ConfigError> {
        let backend_url = env_or("KIOSK_BACKEND_URL", "http://localhost:8000");

        let tenant_id = match env::var("KIOSK_TENANT_ID") {
            Ok(v) if !v.trim().is_empty() => v,
            _ => return Err(ConfigError::Missing("KIOSK_TENANT_ID")),
        };

        // No safe default: a stray fallback here would let the terminal
        // silently authenticate against the backend with a well-known key
        // (whatever the default happened to be) instead of failing loudly at
        // startup — the same fail-closed reasoning as `sam_aid`/
        // `sam_key_entry_no` below, but unconditional (this key is used on
        // every request, not just `KIOSK_READER=pcsc` builds).
        let device_key = require_env("KIOSK_DEVICE_KEY")?;
        let device_id = env_or("KIOSK_DEVICE_ID", "TERMINAL-01");

        let reader = match env_or("KIOSK_READER", "mock")
            .trim()
            .to_ascii_lowercase()
            .as_str()
        {
            "mock" => ReaderKind::Mock,
            "pcsc" => ReaderKind::Pcsc,
            other => {
                return Err(ConfigError::InvalidReader {
                    value: other.to_owned(),
                })
            }
        };

        let sam_reader_substr = env_or("KIOSK_SAM_READER", "SAM");
        let picc_reader_substr = env_or("KIOSK_PICC_READER", "PICC");
        let sam_app_key_no = env_u8_or("KIOSK_SAM_APP_KEY_NO", 0)?;
        let sam_key_entry_ver = env_u8_or("KIOSK_SAM_KEY_ENTRY_VER", 0)?;
        let sam_file_no = env_u8_or("KIOSK_SAM_FILE_NO", 0)?;
        let sam_token_id_len = env_u8_or("KIOSK_SAM_TOKEN_ID_LEN", 16)?;

        // AID and the company SAM key-entry number have no safe default — a
        // wrong guess would silently authenticate against the wrong key
        // (fails closed at the SAM, but only visibly so once real hardware
        // is in the loop). Require them explicitly, but only when a real
        // reader is actually configured — the mock build must keep working
        // with zero extra env vars.
        let (sam_aid, sam_key_entry_no) = if reader == ReaderKind::Pcsc {
            let aid_hex = require_env("KIOSK_SAM_AID")?;
            (
                parse_sam_aid_hex(&aid_hex)?,
                require_u8_env("KIOSK_SAM_KEY_ENTRY_NO")?,
            )
        } else {
            ([0u8; 3], 0)
        };

        Ok(Self {
            backend_url: backend_url.trim_end_matches('/').to_owned(),
            tenant_id,
            device_key,
            device_id,
            reader,
            sam_aid,
            sam_app_key_no,
            sam_key_entry_no,
            sam_key_entry_ver,
            sam_file_no,
            sam_token_id_len,
            sam_reader_substr,
            picc_reader_substr,
        })
    }
}

/// Read an environment variable, falling back to a default when unset or empty.
fn env_or(key: &str, default: &str) -> String {
    match env::var(key) {
        Ok(v) if !v.trim().is_empty() => v,
        _ => default.to_owned(),
    }
}

/// Read a required (non-empty) environment variable.
fn require_env(key: &'static str) -> Result<String, ConfigError> {
    match env::var(key) {
        Ok(v) if !v.trim().is_empty() => Ok(v),
        _ => Err(ConfigError::Missing(key)),
    }
}

/// Read an environment variable as a `u8`, falling back to `default` when
/// unset/empty; a present-but-unparseable value is a hard error.
fn env_u8_or(key: &'static str, default: u8) -> Result<u8, ConfigError> {
    match env::var(key) {
        Ok(v) if !v.trim().is_empty() => v
            .trim()
            .parse::<u8>()
            .map_err(|_| ConfigError::InvalidU8 { key, value: v }),
        _ => Ok(default),
    }
}

/// Read a required environment variable as a `u8`.
fn require_u8_env(key: &'static str) -> Result<u8, ConfigError> {
    let v = require_env(key)?;
    v.trim()
        .parse::<u8>()
        .map_err(|_| ConfigError::InvalidU8 { key, value: v })
}

/// Parse `KIOSK_SAM_AID` (6 hex chars) into 3 bytes.
fn parse_sam_aid_hex(value: &str) -> Result<[u8; 3], ConfigError> {
    let bytes = hex::decode(value.trim()).map_err(|e| ConfigError::InvalidSamAid {
        value: value.to_owned(),
        reason: e.to_string(),
    })?;
    if bytes.len() != 3 {
        return Err(ConfigError::InvalidSamAid {
            value: value.to_owned(),
            reason: format!("must decode to exactly 3 bytes, got {}", bytes.len()),
        });
    }
    let mut out = [0u8; 3];
    out.copy_from_slice(&bytes);
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    /// `Config::from_env` reads process-global environment variables, and
    /// `cargo test` runs `#[test]` functions concurrently by default —
    /// serialize every test below so they don't race on
    /// `env::set_var`/`remove_var`. `unwrap_or_else` recovers from a
    /// poisoned lock (a prior panicking test) rather than cascading the
    /// panic into every subsequent test.
    static ENV_LOCK: Mutex<()> = Mutex::new(());

    fn lock_env() -> std::sync::MutexGuard<'static, ()> {
        ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// SAFETY (test-only): all env mutation in this module is guarded by
    /// [`ENV_LOCK`], so no other thread reads/writes these vars concurrently.
    unsafe fn set(key: &str, value: &str) {
        env::set_var(key, value);
    }

    /// SAFETY (test-only): see [`set`].
    unsafe fn unset(key: &str) {
        env::remove_var(key);
    }

    #[test]
    fn missing_tenant_is_an_error() {
        let _guard = lock_env();
        unsafe {
            unset("KIOSK_TENANT_ID");
        }

        let result = Config::from_env();

        assert!(matches!(
            result,
            Err(ConfigError::Missing("KIOSK_TENANT_ID"))
        ));
    }

    #[test]
    fn missing_device_key_is_an_error() {
        let _guard = lock_env();
        unsafe {
            set("KIOSK_TENANT_ID", "11111111-1111-1111-1111-111111111111");
            unset("KIOSK_READER");
            unset("KIOSK_DEVICE_KEY");
        }

        let result = Config::from_env();

        assert!(matches!(
            result,
            Err(ConfigError::Missing("KIOSK_DEVICE_KEY"))
        ));

        // An all-whitespace value is treated the same as unset (see
        // `require_env`) — a blank key must not silently pass either.
        unsafe {
            set("KIOSK_DEVICE_KEY", "   ");
        }
        let result = Config::from_env();
        assert!(matches!(
            result,
            Err(ConfigError::Missing("KIOSK_DEVICE_KEY"))
        ));

        unsafe {
            unset("KIOSK_TENANT_ID");
            unset("KIOSK_DEVICE_KEY");
        }
    }

    #[test]
    fn mock_reader_never_requires_sam_fields_and_uses_defaults() {
        let _guard = lock_env();
        unsafe {
            set("KIOSK_TENANT_ID", "11111111-1111-1111-1111-111111111111");
            set("KIOSK_DEVICE_KEY", "test-device-key");
            unset("KIOSK_READER");
            unset("KIOSK_SAM_AID");
            unset("KIOSK_SAM_KEY_ENTRY_NO");
            unset("KIOSK_SAM_APP_KEY_NO");
            unset("KIOSK_SAM_TOKEN_ID_LEN");
            unset("KIOSK_SAM_READER");
            unset("KIOSK_PICC_READER");
        }

        let cfg = Config::from_env().unwrap();

        assert_eq!(cfg.device_key, "test-device-key");
        assert_eq!(cfg.reader, ReaderKind::Mock);
        assert_eq!(cfg.sam_aid, [0u8; 3]);
        assert_eq!(cfg.sam_app_key_no, 0);
        assert_eq!(cfg.sam_key_entry_no, 0);
        assert_eq!(cfg.sam_token_id_len, 16);
        assert_eq!(cfg.sam_reader_substr, "SAM");
        assert_eq!(cfg.picc_reader_substr, "PICC");

        unsafe {
            unset("KIOSK_TENANT_ID");
            unset("KIOSK_DEVICE_KEY");
        }
    }

    #[test]
    fn pcsc_reader_requires_sam_aid_and_key_entry_no() {
        let _guard = lock_env();
        unsafe {
            set("KIOSK_TENANT_ID", "11111111-1111-1111-1111-111111111111");
            set("KIOSK_DEVICE_KEY", "test-device-key");
            set("KIOSK_READER", "pcsc");
            unset("KIOSK_SAM_AID");
            unset("KIOSK_SAM_KEY_ENTRY_NO");
        }

        let err = Config::from_env().unwrap_err();
        assert!(matches!(err, ConfigError::Missing("KIOSK_SAM_AID")));

        unsafe {
            set("KIOSK_SAM_AID", "zz"); // not valid hex
        }
        let err = Config::from_env().unwrap_err();
        assert!(matches!(err, ConfigError::InvalidSamAid { .. }));

        unsafe {
            set("KIOSK_SAM_AID", "0102"); // wrong length (2 bytes, need 3)
        }
        let err = Config::from_env().unwrap_err();
        assert!(matches!(err, ConfigError::InvalidSamAid { .. }));

        unsafe {
            set("KIOSK_SAM_AID", "010203");
            unset("KIOSK_SAM_KEY_ENTRY_NO");
        }
        let err = Config::from_env().unwrap_err();
        assert!(matches!(
            err,
            ConfigError::Missing("KIOSK_SAM_KEY_ENTRY_NO")
        ));

        unsafe {
            set("KIOSK_SAM_KEY_ENTRY_NO", "2");
        }
        let cfg = Config::from_env().unwrap();
        assert_eq!(cfg.sam_aid, [0x01, 0x02, 0x03]);
        assert_eq!(cfg.sam_key_entry_no, 2);

        unsafe {
            unset("KIOSK_TENANT_ID");
            unset("KIOSK_DEVICE_KEY");
            unset("KIOSK_READER");
            unset("KIOSK_SAM_AID");
            unset("KIOSK_SAM_KEY_ENTRY_NO");
        }
    }

    #[test]
    fn invalid_sam_numeric_field_is_a_clear_error() {
        let _guard = lock_env();
        unsafe {
            set("KIOSK_TENANT_ID", "11111111-1111-1111-1111-111111111111");
            set("KIOSK_DEVICE_KEY", "test-device-key");
            unset("KIOSK_READER");
            set("KIOSK_SAM_APP_KEY_NO", "not-a-number");
        }

        let err = Config::from_env().unwrap_err();
        assert!(matches!(
            err,
            ConfigError::InvalidU8 {
                key: "KIOSK_SAM_APP_KEY_NO",
                ..
            }
        ));

        unsafe {
            unset("KIOSK_TENANT_ID");
            unset("KIOSK_DEVICE_KEY");
            unset("KIOSK_SAM_APP_KEY_NO");
        }
    }
}
