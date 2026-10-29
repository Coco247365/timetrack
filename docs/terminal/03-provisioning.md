# Provisioning — Schritt für Schritt pro Terminal

> Teil der [Terminal-Serienproduktions-Doku](INDEX.md). Stand 2026-07-16,
> abgeleitet aus dem HW-Bringup 2026-07-15/16.

Voraussetzung: Hardware ist verkabelt wie in
[`02-hardware-verkabelung.md`](02-hardware-verkabelung.md) beschrieben, der Pi
läuft mit Raspberry Pi OS (labwc/Wayland) und ist per SSH erreichbar.

## 1. OS / Kiosk-Deploy (git-push, kein rsync)

Der Kiosk wird per **git-push-Deploy** auf den Pi gebracht — kein manuelles
Kopieren von Binaries:

```bash
git push pi master     # bare Repo + post-receive-Hook auf dem Pi
```

Der `post-receive`-Hook baut daraufhin automatisch:

```bash
cargo build --release --features real-reader
```

— also die "echte Reader"-Variante (nicht die Mock-Variante für lokale
Entwicklung ohne Hardware).

**Autostart:** Das Kiosk-Fenster startet automatisch über labwc (den
Wayland-Compositor des Pi), konfiguriert per `kiosk/pi/labwc-rc.xml`
(Vollbild, kein Fenstertitel, kein Schließen-Shortcut) und
`kiosk/pi/run-kiosk.sh` als Autostart-Kommando. `run-kiosk.sh`:

- setzt Slint/Wayland-Umgebungsvariablen (`SLINT_FULLSCREEN`, `SLINT_BACKEND`),
- sourced `~/kiosk.env`, falls vorhanden (siehe Abschnitt 3 — überlebt
  `git checkout -f` beim nächsten Deploy, weil sie **außerhalb** des
  Git-Worktrees liegt),
- läuft in einer Respawn-Schleife: stürzt der Kiosk ab, startet er nach 1 s
  automatisch neu.

## 2. Terminal-Gerät im Backend registrieren

Bevor der Kiosk mit dem Backend sprechen kann, braucht er einen
**Device-Key**. Den erzeugt ein Admin einmalig über die API:

```bash
curl -X POST https://your-server/api/v1/admin/devices \
  -H "Authorization: Bearer <admin-jwt>" \
  -H "Content-Type: application/json" \
  -d '{"name": "Terminal Werkstatt-Eingang", "location": "Werkstatt"}'
```

Die Antwort enthält den **Klartext-Schlüssel einmalig** (`key`-Feld) — er
wird danach nie wieder angezeigt oder abrufbar sein. **Sofort sichern** und
in `~/kiosk.env` auf dem Pi eintragen (siehe Schritt 3).

## 3. Env-Konfiguration auf dem Pi

Zwei Dateien wirken zusammen:

- `run-kiosk.sh` (Teil des Repos, für alle Terminals gleich): Default-Werte
  wie `KIOSK_TENANT_ID`, `KIOSK_BACKEND_URL`, `KIOSK_READER=pcsc`.
- **`~/kiosk.env`** (liegt **außerhalb** des Git-Worktrees, pro Terminal
  individuell, `chmod 600`): überschreibt/ergänzt die terminal-spezifischen,
  geheimen Werte. Wird von `run-kiosk.sh` per `. "$HOME/kiosk.env"` gesourct
  — **muss** deshalb echte Werte enthalten, sonst läuft der Kiosk mit dem
  Repo-Platzhalter `KIOSK_DEVICE_KEY=dev-terminal` (siehe
  [Troubleshooting](05-troubleshooting.md)).

`~/kiosk.env` (Beispiel):

```bash
export KIOSK_DEVICE_KEY="zdk_<der-echte-schluessel-aus-schritt-2>"
export KIOSK_DEVICE_ID="TERMINAL-WERKSTATT-01"
export KIOSK_SAM_AID="0102fb"          # 6 Hex-Zeichen — AID der personalisierten Fobs, vom Team vorgegeben
export KIOSK_SAM_KEY_ENTRY_NO="1"      # SAM-Schlüssel-Eintrag mit dem Firmen-Schlüssel
```

`run-kiosk.sh` selbst trägt bereits (für alle Terminals gleich, ggf. pro
Standort anpassen):

```bash
export KIOSK_TENANT_ID=<firmen-uuid>
export KIOSK_BACKEND_URL=https://your-server
export KIOSK_READER=pcsc
```

> **Wichtig:** `KIOSK_SAM_AID` und `KIOSK_SAM_KEY_ENTRY_NO` müssen **exakt**
> zu den Werten passen, mit denen die Fobs personalisiert wurden — sonst
> lehnt das Terminal jede Karte ab, obwohl alles korrekt personalisiert
> wurde.

## 4. SAM-Key-Ceremony (nur Referenz)

Der SAM-Sicherheitschip im Reader muss mit dem Firmen-Master-Key geladen
werden, bevor Fobs personalisiert werden können (`ExtSET 0x19`,
`ALLOW_DUMP_SECRET_KEY`). Dieser Schritt ist **sicherheitskritisch** und wird
hier bewusst **nicht** beschrieben.

→ Details und Ablauf stehen im internen Security-Dokument
`docs/terminal-INTERN-sam-security.md` (nicht öffentlich).
Wer die Ceremony durchführen muss, bekommt Zugriff vom Team.

## 5. Verifikation

**Kiosk starten und einen Fob testen:**

1. Pi neu starten (oder `run-kiosk.sh` manuell ausführen) — die Kiosk-UI
   sollte im Vollbild erscheinen.
2. Einen bereits personalisierten und zugewiesenen Fob auflegen → das
   Terminal zeigt eine Preview/Kommen-Gehen-Bestätigung.
3. Fehlt die SAM-Personalisierung noch, siehe
   [`04-workflows.md`](04-workflows.md) für das Fob-Enrollment.

**Device-Auth direkt gegen das Backend testen** (unabhängig vom Kiosk):

```bash
curl -X POST https://your-server/api/v1/clock/nfc/preview \
  -H "X-Device-Api-Key: <der-echte-device-key>" \
  -H "Content-Type: application/json" \
  -d '{"badge_id": "<token-id-eines-zugewiesenen-fobs>"}'
```

Ein `403` bedeutet: Device-Key falsch/inaktiv. Ein `404`/leere Antwort ohne
Fehler bei unbekanntem `badge_id` ist normal (Fob noch nicht zugewiesen).

## Weiterführend

- [`04-workflows.md`](04-workflows.md) — Fob-Enrollment, Zuweisung, täglicher Betrieb
- [`05-troubleshooting.md`](05-troubleshooting.md) — wenn etwas nicht klappt
