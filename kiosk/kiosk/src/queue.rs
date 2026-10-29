//! Offline-Warteschlange für Stempelungen.
//!
//! Erreicht der Kiosk das Backend nicht, wird der Stempel als JSON-Zeile in
//! eine lokale Datei geschrieben (append-only, eine Zeile pro Stempel) und vom
//! Flush-Thread nachgereicht, sobald die Verbindung wieder steht. Die
//! `client_event_id` macht die Nachreichung serverseitig idempotent — ein
//! doppelt gesendeter Eintrag wird nie doppelt gebucht.
//!
//! Lehnt das Backend einen Eintrag beim Nachreichen ENDGÜLTIG ab (z. B.
//! unbekannter Ausweis), wird er nicht stillschweigend verworfen: er landet
//! zusätzlich als Zeile in `discarded-queue.jsonl` (gleiches Verzeichnis, via
//! [`OfflineQueue::discard`]) — ein Dead-Letter-Log, das eine echte Stempelung
//! nachvollziehbar hält, statt sie beim ersten Fehlversuch spurlos zu
//! verlieren (siehe `.claude/rules/append-only-ledger-and-events.md`).

use std::fs::{self, File, OpenOptions};
use std::io::{BufRead, BufReader, Write};
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;

use serde::{Deserialize, Serialize};

/// Ein wartender Stempel.
///
/// `event_type == None` bedeutet: die Aktion war beim Tap unbekannt (Backend
/// war schon für die Vorschau nicht erreichbar) — der Eintrag wird über
/// `/nfc/auto` nachgereicht, das Kommen/Gehen aus dem Serverzustand ableitet.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct QueuedPunch {
    /// Idempotenz-Schlüssel, auf diesem Gerät erzeugt.
    pub client_event_id: String,
    pub badge_id: String,
    pub event_type: Option<String>,
    /// RFC-3339-Zeitstempel (UTC) des ursprünglichen Taps.
    pub event_time: String,
}

/// Ein endgültig abgelehnter Eintrag (dead-letter), zusammen mit der
/// Backend-Fehlermeldung und dem Zeitpunkt des Verwerfens. Reines
/// Diagnose-/Audit-Artefakt — fließt nicht mehr in die aktive Warteschlange
/// ein, wird nie automatisch nachgereicht.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DiscardedPunch {
    #[serde(flatten)]
    pub punch: QueuedPunch,
    /// Menschenlesbare Fehlermeldung des Backends (`ApiError::to_string()`).
    pub reason: String,
    /// Unix-Millis, wann der Eintrag verworfen wurde.
    pub discarded_at_millis: u128,
}

/// Zähler für eindeutige `client_event_id`s innerhalb eines Prozesslaufs.
static SEQ: AtomicU64 = AtomicU64::new(0);

/// Erzeuge eine gerätelokal eindeutige Event-ID (Unix-Millis + Laufnummer).
#[must_use]
pub fn new_client_event_id() -> String {
    let millis = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0);
    let seq = SEQ.fetch_add(1, Ordering::Relaxed);
    format!("kiosk-{millis}-{seq}")
}

/// Dateibasierte Warteschlange (JSONL). Alle Zugriffe laufen über ein Mutex,
/// damit UI-Thread (append) und Flush-Thread (drain) sich nicht überlappen.
pub struct OfflineQueue {
    path: PathBuf,
    lock: Mutex<()>,
}

impl OfflineQueue {
    /// Queue an `KIOSK_QUEUE_PATH` (Default: `offline-queue.jsonl` im
    /// Arbeitsverzeichnis) öffnen. Die Datei entsteht erst beim ersten Eintrag.
    #[must_use]
    pub fn from_env() -> Self {
        let path = std::env::var("KIOSK_QUEUE_PATH")
            .map(PathBuf::from)
            .unwrap_or_else(|_| PathBuf::from("offline-queue.jsonl"));
        Self {
            path,
            lock: Mutex::new(()),
        }
    }

    /// Einen Stempel anhängen. Fehler werden nur geloggt — ein voller/kaputter
    /// Datenträger darf den Kiosk nicht zum Absturz bringen.
    pub fn append(&self, punch: &QueuedPunch) {
        let _guard = self.lock.lock().unwrap_or_else(|e| e.into_inner());
        let line = match serde_json::to_string(punch) {
            Ok(l) => l,
            Err(e) => {
                eprintln!("[queue] Eintrag nicht serialisierbar: {e}");
                return;
            }
        };
        let result = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.path)
            .and_then(|mut f| {
                writeln!(f, "{line}")?;
                f.sync_all()
            });
        match result {
            Ok(()) => eprintln!(
                "[queue] gespeichert: {} {} @ {}",
                punch.badge_id,
                punch.event_type.as_deref().unwrap_or("auto"),
                punch.event_time
            ),
            Err(e) => eprintln!("[queue] Schreiben fehlgeschlagen ({:?}): {e}", self.path),
        }
    }

    /// Alle wartenden Einträge in Datei-Reihenfolge (= chronologisch) lesen.
    /// Kaputte Zeilen werden übersprungen und geloggt.
    #[must_use]
    pub fn load(&self) -> Vec<QueuedPunch> {
        let _guard = self.lock.lock().unwrap_or_else(|e| e.into_inner());
        self.load_unlocked()
    }

    fn load_unlocked(&self) -> Vec<QueuedPunch> {
        let Ok(file) = File::open(&self.path) else {
            return Vec::new();
        };
        BufReader::new(file)
            .lines()
            .map_while(Result::ok)
            .filter(|l| !l.trim().is_empty())
            .filter_map(|l| match serde_json::from_str::<QueuedPunch>(&l) {
                Ok(p) => Some(p),
                Err(e) => {
                    eprintln!("[queue] kaputte Zeile übersprungen: {e}");
                    None
                }
            })
            .collect()
    }

    /// Anzahl wartender Einträge.
    #[must_use]
    pub fn len(&self) -> usize {
        self.load().len()
    }

    /// Pfad der Dead-Letter-Datei: gleiches Verzeichnis wie die Warteschlange,
    /// Dateiname `discarded-queue.jsonl`.
    fn discard_path(&self) -> PathBuf {
        match self.path.parent() {
            Some(dir) if !dir.as_os_str().is_empty() => dir.join("discarded-queue.jsonl"),
            _ => PathBuf::from("discarded-queue.jsonl"),
        }
    }

    /// Einen Eintrag ENDGÜLTIG verwerfen: statt ihn stillschweigend zu
    /// löschen, wird er zusammen mit `reason` (die Backend-Fehlermeldung) als
    /// [`DiscardedPunch`]-Zeile an die Dead-Letter-Datei angehängt — erst
    /// danach aus der aktiven Warteschlange entfernt. So geht ein einmal
    /// getippter Stempel nie spurlos verloren, selbst wenn das Backend ihn
    /// dauerhaft ablehnt (z. B. unbekannter Ausweis).
    ///
    /// Schlägt schon das Anhängen an die Dead-Letter-Datei fehl (Datenträger
    /// voll/schreibgeschützt), bleibt der Eintrag sicherheitshalber in der
    /// aktiven Warteschlange, statt ihn spurlos zu verlieren — er wird beim
    /// nächsten Flush erneut versucht.
    pub fn discard(&self, punch: &QueuedPunch, reason: &str) {
        if self.append_dead_letter(punch, reason) {
            self.remove(&punch.client_event_id);
        }
    }

    /// Returns `true` when the dead-letter line was written successfully.
    fn append_dead_letter(&self, punch: &QueuedPunch, reason: &str) -> bool {
        let _guard = self.lock.lock().unwrap_or_else(|e| e.into_inner());
        let discarded_at_millis = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_millis())
            .unwrap_or(0);
        let entry = DiscardedPunch {
            punch: punch.clone(),
            reason: reason.to_owned(),
            discarded_at_millis,
        };
        let line = match serde_json::to_string(&entry) {
            Ok(l) => l,
            Err(e) => {
                eprintln!("[queue] Dead-Letter-Eintrag nicht serialisierbar: {e}");
                return false;
            }
        };
        let path = self.discard_path();
        let result = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&path)
            .and_then(|mut f| {
                writeln!(f, "{line}")?;
                f.sync_all()
            });
        match result {
            Ok(()) => {
                eprintln!(
                    "[queue] endgültig verworfen -> {:?}: {} ({reason})",
                    path, punch.client_event_id
                );
                true
            }
            Err(e) => {
                eprintln!(
                    "[queue] Dead-Letter-Schreiben fehlgeschlagen ({:?}): {e} — Eintrag {} \
                     bleibt sicherheitshalber in der aktiven Warteschlange",
                    path, punch.client_event_id
                );
                false
            }
        }
    }

    /// Einen erledigten (oder endgültig abgelehnten) Eintrag entfernen: die
    /// Datei wird ohne diese `client_event_id` neu geschrieben.
    pub fn remove(&self, client_event_id: &str) {
        let _guard = self.lock.lock().unwrap_or_else(|e| e.into_inner());
        let remaining: Vec<QueuedPunch> = self
            .load_unlocked()
            .into_iter()
            .filter(|p| p.client_event_id != client_event_id)
            .collect();
        let result = if remaining.is_empty() {
            fs::remove_file(&self.path).or_else(|e| {
                if e.kind() == std::io::ErrorKind::NotFound {
                    Ok(())
                } else {
                    Err(e)
                }
            })
        } else {
            let tmp = self.path.with_extension("jsonl.tmp");
            let write = || -> std::io::Result<()> {
                let mut f = File::create(&tmp)?;
                for p in &remaining {
                    let line = serde_json::to_string(p)
                        .map_err(|e| std::io::Error::new(std::io::ErrorKind::Other, e))?;
                    writeln!(f, "{line}")?;
                }
                f.sync_all()?;
                fs::rename(&tmp, &self.path)
            };
            write()
        };
        if let Err(e) = result {
            eprintln!("[queue] Entfernen fehlgeschlagen ({client_event_id}): {e}");
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_queue(name: &str) -> OfflineQueue {
        let mut path = std::env::temp_dir();
        path.push(format!(
            "kiosk-queue-test-{name}-{}.jsonl",
            std::process::id()
        ));
        let _ = fs::remove_file(&path);
        OfflineQueue {
            path,
            lock: Mutex::new(()),
        }
    }

    fn punch(id: &str, event_type: Option<&str>) -> QueuedPunch {
        QueuedPunch {
            client_event_id: id.to_owned(),
            badge_id: "BADGE1".to_owned(),
            event_type: event_type.map(str::to_owned),
            event_time: "2026-07-02T08:00:00Z".to_owned(),
        }
    }

    #[test]
    fn append_load_remove_roundtrip() {
        let q = temp_queue("roundtrip");
        assert_eq!(q.len(), 0);

        q.append(&punch("a", Some("PUNCH_IN")));
        q.append(&punch("b", None));
        let loaded = q.load();
        assert_eq!(loaded.len(), 2);
        assert_eq!(loaded[0].client_event_id, "a");
        assert_eq!(loaded[0].event_type.as_deref(), Some("PUNCH_IN"));
        assert_eq!(loaded[1].event_type, None);

        q.remove("a");
        let loaded = q.load();
        assert_eq!(loaded.len(), 1);
        assert_eq!(loaded[0].client_event_id, "b");

        q.remove("b");
        assert_eq!(q.len(), 0);
        assert!(!q.path.exists());
    }

    #[test]
    fn corrupt_lines_are_skipped() {
        let q = temp_queue("corrupt");
        q.append(&punch("ok", Some("PUNCH_OUT")));
        {
            let mut f = OpenOptions::new().append(true).open(&q.path).unwrap();
            writeln!(f, "{{kaputt").unwrap();
        }
        q.append(&punch("ok2", None));
        let loaded = q.load();
        assert_eq!(loaded.len(), 2);
        let _ = fs::remove_file(&q.path);
    }

    #[test]
    fn client_event_ids_are_unique() {
        let a = new_client_event_id();
        let b = new_client_event_id();
        assert_ne!(a, b);
        assert!(a.starts_with("kiosk-"));
    }

    #[test]
    fn discard_writes_dead_letter_and_removes_from_active_queue() {
        let q = temp_queue("discard");
        let discard_path = q.discard_path();
        let _ = fs::remove_file(&discard_path);

        q.append(&punch("a", Some("PUNCH_IN")));
        q.append(&punch("b", Some("PUNCH_OUT")));
        assert_eq!(q.len(), 2);

        q.discard(&punch("a", Some("PUNCH_IN")), "unbekannter Ausweis");

        // Removed from the active queue...
        let loaded = q.load();
        assert_eq!(loaded.len(), 1);
        assert_eq!(loaded[0].client_event_id, "b");

        // ...but preserved in the dead-letter file with the reason attached.
        let dead_letters: Vec<DiscardedPunch> = BufReader::new(File::open(&discard_path).unwrap())
            .lines()
            .map_while(Result::ok)
            .filter(|l| !l.trim().is_empty())
            .map(|l| serde_json::from_str(&l).unwrap())
            .collect();
        assert_eq!(dead_letters.len(), 1);
        assert_eq!(dead_letters[0].punch.client_event_id, "a");
        assert_eq!(dead_letters[0].reason, "unbekannter Ausweis");

        let _ = fs::remove_file(&discard_path);
        let _ = fs::remove_file(&q.path);
    }
}
