# Troubleshooting

> Teil der [Terminal-Serienproduktions-Doku](INDEX.md). Stand 2026-07-16,
> abgeleitet aus dem HW-Bringup 2026-07-15/16.

| Symptom | Wahrscheinliche Ursache | Fix |
|---|---|---|
| Touch reagiert nicht / kein Touch-Gerät in `lsusb` | Dedizierte USB-C-Touch-Buchse des Panels nicht oder falsch verkabelt; Charge-only-Kabel statt Datenkabel; Touch-Kabel am Pi-5-USB-C (Power-only) statt USB-A angeschlossen | Verkabelung gemäß [`02-hardware-verkabelung.md`](02-hardware-verkabelung.md) prüfen: Panel-TOUCH-USB-C → datenfähiges Kabel → Pi-**USB-A**. Mit `lsusb`/`libinput list-devices` verifizieren |
| Maus-Cursor sichtbar auf dem Terminal | Nur relevant, wenn tatsächlich eine Maus angeschlossen ist — Cursor-Hide ist app-seitig implementiert, greift aber nur ohne aktive Maus-Bewegung | Maus abziehen, falls nicht benötigt; ist kein Touch-Problem |
| `register-to-pool` liefert `404` | Backend-Version auf `.176` noch nicht auf dem aktuellen Stand deployt (Endpoint fehlt) | Beim Team nachfragen, ob der aktuelle Backend-Release live ist |
| SAM antwortet mit `6985` | SAM ist nicht aktiviert/gekeyt (Key-Ceremony fehlt oder unvollständig) | Ceremony gemäß internem Security-Dokument durchführen (Verweis in [`03-provisioning.md`](03-provisioning.md) §4) |
| SAM antwortet mit `6A84` | `Le`-Regel ist kommando-spezifisch (bekannter Stolperstein) — kein Terminal-Bedienfehler | An das Entwickler-Team melden, nicht selbst am Wire-Format herumprobieren |
| Fob-Personalisierung schlägt fehl mit "DUPLICATE" | Der aufgelegte Fob ist nicht mehr leer (schon einmal personalisiert) | Anderen, wirklich leeren Fob verwenden; bereits personalisierte Fobs nicht erneut personalisieren |
| Kiosk-Buttons reagieren generell nicht auf Berührung | Fast immer Touch-Verkabelung, nicht die App — siehe erste Zeile dieser Tabelle | Verkabelung prüfen, bevor an der Software gesucht wird |
| Device-Key ist noch `dev-terminal` (Platzhalter) | `~/kiosk.env` auf dem Pi fehlt oder wurde nicht mit dem echten Device-Key aus der Backend-Registrierung befüllt | Echten Key gemäß [`03-provisioning.md`](03-provisioning.md) §2–3 in `~/kiosk.env` eintragen |

Bei Symptomen außerhalb dieser Tabelle: nicht raten, Team fragen — besonders
bei allem rund um SAM/DESFire-Fehlercodes, da diese meist ein Wire-Format-
Detail betreffen und kein Bedienfehler vor Ort sind.
