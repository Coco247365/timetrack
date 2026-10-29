# Terminal-Serienproduktion — Übersicht

> Abgeleitet aus dem HW-Bringup 2026-07-15/16. Zielgruppe: Team-Kollegen und
> Werkstudenten, die weitere Wand-Terminals bauen, verkabeln und in Betrieb
> nehmen.

Ein **Terminal** ist ein Wand-montiertes Gerät (Raspberry Pi + Touchscreen +
NFC-Leser mit Sicherheitschip), an dem Mitarbeiter per Chip-Anhänger ("Fob")
kommen/gehen stempeln. Es prüft jeden Chip kryptografisch, bevor es eine
Stempelung entgegennimmt, und schickt sie ans zentrale Backend.

## Inhalt

| Dokument | Inhalt |
|---|---|
| [`01-ueberblick-architektur.md`](01-ueberblick-architektur.md) | Was ein Terminal ist, welche Komponenten es hat, Security-Modell (High-Level) |
| [`02-hardware-verkabelung.md`](02-hardware-verkabelung.md) | Stückliste + die kritische Touch-Verkabelung (häufigster Stolperstein) |
| [`03-provisioning.md`](03-provisioning.md) | Schritt-für-Schritt: neues Terminal von Null auf betriebsbereit |
| [`04-workflows.md`](04-workflows.md) | Betrieb: Fob-Enrollment, Mitarbeiter-Zuweisung, täglicher Punch |
| [`05-troubleshooting.md`](05-troubleshooting.md) | Symptom → Ursache → Fix, tabellarisch |

## Wichtiger Hinweis zur Sicherheit

Die **SAM-Key-Ceremony** (wie der Firmen-Master-Schlüssel erzeugt, in den
Sicherheitschip geladen und gesichert wird) ist **absichtlich nicht** Teil
dieser Dokumente. Das ist sicherheitskritisches Schlüssel-Handling und liegt
ausschließlich im internen, nicht-öffentlichen Security-Dokument. Wer die
Ceremony durchführen muss, bekommt das Dokument vom Team.
