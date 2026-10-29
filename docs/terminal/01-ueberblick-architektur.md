# Überblick & Architektur

> Teil der [Terminal-Serienproduktions-Doku](INDEX.md). Stand 2026-07-16,
> abgeleitet aus dem HW-Bringup 2026-07-15/16.

## Was ist ein Terminal?

Ein Terminal ist ein an der Wand montiertes Stempel-Gerät, bestehend aus:

- **Raspberry Pi 5** — der Rechner im Terminal.
- **Waveshare 8DP-CAPLCD** — 8″-Touchscreen (HDMI-Bild + USB-C-Touch), zeigt
  die Kiosk-Oberfläche.
- **ACR1252U** (Dual-Reader) — der NFC-Kartenleser, mit **zwei** logischen
  Slots: PICC (liest die Mitarbeiter-Fobs) und SAM (steckt den
  Sicherheitschip).
- **MF4SAM3** (SAM AV3) — ein manipulationssicherer Sicherheitschip im
  SAM-Slot des Readers; hält den kryptografischen Firmen-Schlüssel und macht
  die Authentizitätsprüfung jedes Fobs.

Die Software auf dem Pi ist der **Kiosk** (Rust/Slint), der mit dem
zentralen **Backend** (FastAPI, `https://your-server`) spricht. Zusätzlich
gibt es die **WebApp**, über die Admins/HR Mitarbeiter, Fobs und
Terminal-Geräte verwalten.

```
 Fob (Mitarbeiter) ── NFC ──> ACR1252U (PICC-Slot)
                                   │
                            (Challenge-Response)
                                   │
                              SAM-Slot ── MF4SAM3 (Firmen-Schlüssel-Tresor)
                                   │
                             Kiosk (Pi, Rust/Slint)
                                   │
                        HTTPS (X-Device-Api-Key)
                                   │
                              Backend (.176)
                                   │
                               WebApp (Admin/HR)
```

## Security-Modell (High-Level)

Ältere, einfache NFC-Chips lassen sich in Sekunden klonen — man liest nur
eine offene Seriennummer (**UID**) aus. Das reicht für Zeiterfassung **nicht**,
weil eine Kopie ("Buddy-Punching") niemand erkennen würde.

Die Terminals verwenden deshalb **DESFire EV2**-Chips zusammen mit dem
**SAM** im Reader:

- Jeder Fob trägt einen **verschlüsselten, per-Fob-diversifizierten Token**.
  "Diversifiziert" heißt: jeder Chip bekommt seinen **eigenen** Schlüssel,
  abgeleitet aus dem Firmen-Hauptschlüssel + der Chip-Seriennummer. Der
  Hauptschlüssel selbst verlässt den SAM **nie im Klartext**.
- Beim Auflegen stellt der SAM dem Fob eine kryptografische Prüffrage
  (Challenge-Response). Nur ein Fob mit dem passenden, diversifizierten
  Schlüssel kann korrekt antworten.
- **Ein bloßer UID-Klon nützt nichts** — ohne SAM + den richtigen Schlüssel
  ist die Authentifizierung nicht zu bestehen. Der Token selbst ist
  **nicht** ohne SAM/Schlüssel lesbar, auch nicht mit einem Handy.
- Die **Chip-UID** (die klassische, nicht-geheime Seriennummer) bleibt
  weiterhin auslesbar — z. B. per Handy-App (NXP TagInfo). Das ist
  **gewollt und unkritisch**: die UID dient nur der **Identifikation**
  (z. B. um einen Fob im Web wiederzufinden), **nicht** der Authentifizierung.
  Sie zu kennen erlaubt keine Stempelung.

Details zur Schlüssel-Erzeugung, -Verteilung und -Aufbewahrung (die
"Ceremony") sind **sicherheitskritisch** und liegen bewusst **nicht** hier,
sondern gehören in ein internes Security-Dokument des Betreibers. Die
Werkzeuge dafür (`sam_ceremony`) liegen im Kiosk-Crate.

## Software-Komponenten

| Komponente | Sprache/Framework | Rolle |
|---|---|---|
| **Kiosk** | Rust + Slint | Läuft auf dem Terminal-Pi, steuert Reader + SAM, zeigt die Touch-UI, spricht per HTTPS mit dem Backend |
| **Backend** | FastAPI (Python), `.176` | Zentrale API, Datenhaltung, Device-Auth, Mitarbeiter-/Fob-Verwaltung |
| **WebApp** | Nuxt | Admin-/HR-Oberfläche: NFC-Karten zuweisen, Terminal-Geräte registrieren |

**Offline-Punch-Queue:** Ist das Backend gerade nicht erreichbar (Netzwerk
weg), puffert der Kiosk Stempelungen lokal (`kiosk-offline-queue.jsonl`) und
reicht sie nach, sobald die Verbindung wieder da ist. Am Terminal selbst wird
sonst nichts dauerhaft gespeichert.

## Weiterführend

- [`02-hardware-verkabelung.md`](02-hardware-verkabelung.md) — Stückliste + Verkabelung
- [`03-provisioning.md`](03-provisioning.md) — ein neues Terminal aufsetzen
