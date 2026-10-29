# SAM/DESFire-Inbetriebnahme — Vor-Ort-Anleitung (Pilot)

> **Pilot-Anleitung.** Diese Krypto (SAM-Chip + DESFire-Karten) läuft mit
> diesem Einsatz zum **ersten Mal gegen echte Hardware**. Das heißt: dieser
> Lauf ist nicht nur Inbetriebnahme, sondern **gleichzeitig der erste
> Praxistest der Software**. Jeder Fehler ist wertvoll — bitte **melden, nicht
> stillschweigend nochmal versuchen** (siehe Abschnitt 8). Diese Anleitung
> beschreibt nur die praktischen Schritte vor Ort. Das große Bild (Architektur,
> Bedrohungsmodell, warum SAM statt einfacher Karten) steht separat im
> internen Architektur-Dokument (`docs/architektur-nfc-sam.md`, auf `master`)
> — hier wird es **nicht** wiederholt. In dieses Dokument gehören **keine**
> echten Schlüssel, Passwörter oder Backup-Anteile — die entstehen erst vor
> Ort und bleiben ausschließlich auf Papier/im Safe.

## 1. Überblick

Die neuen Chips (DESFire EV2) lassen sich nicht kopieren oder fälschen — im
Gegensatz zu den einfachen Karten, die man in Sekunden klonen kann. Der
Trick: ein kleiner Sicherheitschip im Terminal-Kartenleser, der **SAM**
("Secure Access Module"), ist der Tresor für den einen geheimen
Firmenschlüssel. Beim Auflegen einer Karte stellt das Terminal über den SAM
eine kryptografische Prüffrage ("Challenge-Response") — nur eine Karte, die
mit genau diesem Firmenschlüssel personalisiert wurde, kann korrekt
antworten. Ein fremder oder geklonter Chip (auch eine beliebige andere
DESFire-Karte, Kreditkarte oder ein Handy) fällt bei dieser Prüfung durch und
wird **abgelehnt** — es gibt keine Stempelung "notfalls trotzdem".

## 2. Voraussetzungen

**Hardware vor Ort:**
- ACR1252U-M1-Kartenleser mit SAM-Steckplatz (zwei Antennen/Slots: PICC =
  Karten aufhalten, SAM = Sicherheitschip stecken)
- Der SAM-Chip (vom Admin vorbereitet oder von Ihnen mitgebracht)
- Die ca. 50 leeren DESFire-EV2-Fobs für den Piloten
- Der Terminal-Pi (an Strom, im Firmennetz, mit dem Kartenleser verbunden)

**Zugang:**
- SSH-Zugang auf den Pi (Hostname/IP + Login vom Team)
- Das Terminal ist bereits als Gerät im Backend registriert (Device-Key
  existiert schon — der wird auf dem Pi in `~/zeit-kiosk/run-kiosk.sh`
  gesetzt, siehe dort; er wird in dieser Anleitung **nicht** wiederholt)
- Backend ist vom Pi aus erreichbar (`KIOSK_BACKEND_URL`, ebenfalls schon in
  `run-kiosk.sh` hinterlegt)

**Die relevanten Umgebungsvariablen** (müssen auf dem Pi gesetzt sein, bevor
das fertige Kiosk-Programm läuft — Details in Schritt 3–4):

| Variable | Bedeutung |
|---|---|
| `KIOSK_TENANT_ID` | Firmen-ID im Backend (Pflicht) |
| `KIOSK_DEVICE_KEY` | Geräte-Schlüssel dieses Terminals (Pflicht) |
| `KIOSK_BACKEND_URL` | Adresse des Backend-Servers |
| `KIOSK_READER` | `pcsc` (echter Leser statt Simulation) |
| `KIOSK_SAM_READER` | Text-Ausschnitt des SAM-Lesernamens (Default `SAM`) |
| `KIOSK_PICC_READER` | Text-Ausschnitt des Karten-Lesernamens (Default `PICC`) |
| `KIOSK_SAM_AID` | 6-stelliger Hex-Code der Anwendung auf der Karte (Pflicht bei `pcsc`) |
| `KIOSK_SAM_KEY_ENTRY_NO` | Nummer des SAM-Schlüsseleintrags mit dem Firmenschlüssel (Pflicht bei `pcsc`) |
| `KIOSK_SAM_APP_KEY_NO` | Schlüssel-Nummer auf der Karten-App (Default `0`) |
| `KIOSK_SAM_FILE_NO` | Dateinummer der Token-ID auf der Karte (Default `0`) |

**Wichtig:** Die Werte für `KIOSK_SAM_AID`, `KIOSK_SAM_KEY_ENTRY_NO`,
`KIOSK_SAM_APP_KEY_NO` und `KIOSK_SAM_FILE_NO` müssen **exakt** zu den
Werten passen, die bei der Personalisierung (Schritt 3) verwendet wurden —
sonst lehnt das Terminal jede Karte ab, obwohl alles korrekt personalisiert
wurde. Bei diesem Piloten legt das Team die konkreten Werte vorab fest
(Konvention: Key-Entry `1`, App-Key `0`, Datei `0` — die genaue AID bekommen
Sie vom Team). **Prüfen Sie vor Beginn**, ob `~/zeit-kiosk/run-kiosk.sh` auf
dem Pi diese vier Variablen bereits enthält — falls nicht, müssen sie dort
ergänzt werden, bevor Schritt 5 funktionieren kann (siehe Hinweis in
Abschnitt 4).

## 3. Schritt 1 — SAM einsetzen

1. Terminal vom Strom nehmen bzw. den Kartenleser vom Pi trennen (USB).
2. Den SAM-Chip **vorsichtig** in den SAM-Steckplatz des ACR1252U-M1
   schieben — die goldenen Kontakte zeigen nach unten/zum Leser, die
   abgeschrägte Ecke des Chips zeigt in Einführrichtung (wie bei einer
   SIM-Karte). Der Chip muss ganz einrasten, kein Spalt sichtbar.
3. Kartenleser wieder per USB an den Pi anschließen (bzw. Strom wieder
   anschließen).
4. Per SSH auf den Pi verbinden und prüfen, ob zwei Leser sichtbar sind
   (einer für Karten, einer für den SAM):
   ```bash
   pcsc_scan
   ```
   Erwartet: zwei Zeilen mit Lesernamen, die auf den PC/SC-Dienst
   `pcscd` reagieren, z. B. etwas mit "PICC" und etwas mit "SAM" im Namen
   (das Terminal-Programm erkennt die Leser über einen Textausschnitt im
   Namen — Default `PICC` bzw. `SAM`, siehe `KIOSK_PICC_READER` /
   `KIOSK_SAM_READER` oben). Zum Beenden `Strg+C`.
   - Kein zweiter Leser sichtbar → SAM ist nicht erkannt, siehe
     Fehlerbild-Tabelle unten (Abschnitt 9).

## 4. Schritt 2 — Key-Ceremony (Firmenschlüssel in den SAM laden)

**Diesen Schritt macht in der Regel der Admin vorab** (idealerweise **vor**
dem Vor-Ort-Termin, damit der SAM bereits fertig geschlüsselt ankommt). Falls
er vor Ort ansteht, hier der Ablauf mit `sam_ceremony`
(`kiosk/kiosk/src/bin/sam_ceremony.rs`):

1. Ins Kiosk-Quellverzeichnis auf dem Pi wechseln und mit Hardware-Unterstützung
   bauen (einmalig, dauert ein paar Minuten):
   ```bash
   cd ~/zeit-kiosk-src/kiosk/kiosk
   cargo build --release --features real-reader
   ```
   Voraussetzung dafür: die PC/SC-Systembibliothek muss installiert sein
   (auf Raspberry Pi OS/Debian üblicherweise `libpcsclite-dev` zum Bauen und
   der laufende Dienst `pcscd` zur Laufzeit — falls `cargo build` mit einer
   "pcsclite not found"-Meldung abbricht, ist das der erste Verdacht).
2. Die eigentliche Ceremony:
   ```bash
   ./target/release/sam_ceremony key-company \
     --host-key-file <pfad-zum-host-schluessel> \
     --entry-no 1 \
     --sam SAM \
     --backup-dir ~/sam-backup-<firmenname>
   ```
   - `--host-key-file`: Pfad zu einer Datei mit dem SAM-Werksschlüssel (vom
     Team bereitgestellt — 32 Hex-Zeichen). **Nicht** in dieses Dokument
     eintragen.
   - `--entry-no 1`: in welchen SAM-Schlüsseleintrag der Firmenschlüssel
     geschrieben wird — muss zu `KIOSK_SAM_KEY_ENTRY_NO` passen.
   - `--sam SAM`: Textausschnitt des SAM-Lesernamens (aus Schritt 1).
   - `--backup-dir`: Ordner, in den die Backup-Anteile geschrieben werden.
   - Ohne `--company-key-hex` erzeugt das Werkzeug automatisch einen neuen,
     zufälligen Firmenschlüssel.
3. Das Tool gibt bei Erfolg u. a. aus: `host authenticated, ChangeKeyEntry
   accepted (9000)` und `SAM target ...: OK`, sowie am Ende `N/N SAM(s)
   erfolgreich geschlüsselt`.
4. **Backup sofort sichern:** Im `--backup-dir` liegen mehrere Dateien
   (`share-X-of-N-kK.txt`, standardmäßig 5 Anteile, 3 davon reichen zur
   Wiederherstellung). **Jeden Anteil an einem anderen sicheren Ort
   aufbewahren** (z. B. Firmensafe, HR-Büro, externer Ort) — **niemals alle
   zusammen an einem Ort**, sonst ist der Sinn der Aufteilung hinfällig.
   Einzelne Anteile allein verraten **nichts** über den Schlüssel.
5. **Warum das Backup so wichtig ist:** Der Klartext-Firmenschlüssel
   existiert nur ganz kurz, während dieses Befehls, im Speicher dieser
   Station — danach nirgends mehr, außer eben in den (aufgeteilten)
   Backup-Dateien. **Geht der SAM verloren oder kaputt und es gibt kein
   Backup, sind alle bereits ausgegebenen Karten wertlos** — dann muss jeder
   Mitarbeiter neu personalisiert werden. Mit Backup: ein Ersatz-SAM lässt
   sich mit `sam_ceremony reconstruct --share <datei1> --share <datei2>
   --share <datei3>` (3 von 5 Anteilen genügen) wieder auf denselben
   Schlüssel bringen.
6. **Gap-Hinweis:** Prüfen Sie an dieser Stelle auch gleich
   `~/zeit-kiosk/run-kiosk.sh` auf dem Pi — die vier `KIOSK_SAM_*`-Variablen
   aus Abschnitt 2 müssen dort mit denselben Werten eingetragen sein, die
   Sie hier verwendet haben (`--entry-no` → `KIOSK_SAM_KEY_ENTRY_NO`).
   Fehlen sie, ergänzen Sie die Zeilen jetzt (vor Schritt 5).
7. **Diagnose-Tracing (`SAM_OFF`/`SAM_APDU_DEBUG`) — nur auf der Werkbank:**
   Diese Env-Vars drucken Schlüsselmaterial/Klartext-APDUs auf stderr und
   funktionieren nur, wenn zusätzlich mit dem Cargo-Feature `sam-debug`
   gebaut wurde (`cargo build --release --features "real-reader,sam-debug"`)
   — der reguläre Produktions-Build (ohne `sam-debug`) ignoriert beide
   Variablen vollständig, auch wenn sie versehentlich gesetzt sind. Ein
   Produktions-Terminal (`run-kiosk.sh`/Autostart) wird deshalb **immer ohne**
   `sam-debug` gebaut. Läuft ein Terminal doch einmal testweise mit
   `sam-debug` (z. B. während dieser Ceremony), stellen Sie zusätzlich sicher,
   dass stderr **nicht** dauerhaft mitgeloggt wird (kein `journalctl`-Backend,
   keine persistente Log-Datei) — die Ausgabe ist nur für den Moment des
   Bench-Laufs gedacht.

## 5. Schritt 3 — Fobs personalisieren

Jeder leere Fob wird einmal "fertig gemacht" (bekommt den vom SAM abgeleiteten
Kartenschlüssel + eine zufällige Token-ID) und landet danach automatisch im
Karten-Pool im Backend. Werkzeug: `sam_personalize`
(`kiosk/kiosk/src/bin/sam_personalize.rs`).

```bash
KIOSK_BACKEND_URL=<backend-url> \
KIOSK_TENANT_ID=<firmen-id> \
KIOSK_DEVICE_KEY=<geraete-schluessel> \
./target/release/sam_personalize run \
  --sam SAM --picc PICC \
  --company-key-entry-no 1 \
  --default-key-entry-no 0 \
  --aid 010203 \
  --count 50
```

- `--company-key-entry-no`: derselbe Wert wie `--entry-no` in Schritt 2
  (`KIOSK_SAM_KEY_ENTRY_NO`).
- `--default-key-entry-no`: SAM-Eintrag mit dem Werks-Standardschlüssel der
  leeren Fobs (vom Team vorgegeben).
- `--aid`: 6-stellige Hex-Anwendungs-ID (vom Team vorgegeben — muss zu
  `KIOSK_SAM_AID` passen).
- `--count 50`: Anzahl der Fobs in diesem Batch — bei 50 Fobs also `--count
  50`; bei Bedarf mehrfach mit kleineren Zahlen ausführen.
- Die drei `KIOSK_*`-Variablen davor sind nötig, weil das Werkzeug am Ende
  automatisch alle erfolgreichen Karten per Backend-Aufruf in den Pool
  einträgt (`register-to-pool`).

**Ablauf:** Das Programm zeigt `--- fob 1/50: tap now ---` — legen Sie den
ersten leeren Fob auf den Kartenleser (PICC-Slot, nicht SAM-Slot!). Nach
kurzer Zeit erscheint `fob 1/50: OK`, dann sofort die Aufforderung für den
nächsten. Nehmen Sie den fertigen Fob ab und legen Sie den nächsten auf,
sobald `tap now` für die nächste Nummer erscheint. Am Ende:
`summary: 50/50 fob(s) personalisiert` und
`registering 50 token-id(s) with the backend...` gefolgt von
`pooled=50 ... pool_size=...`.

- Schlägt ein einzelner Fob fehl (`fob N/50: FAILED: ...`), läuft der Batch
  mit dem nächsten Fob weiter — der fehlgeschlagene Fob landet **nicht** im
  Pool und kann einfach erneut aufgelegt werden (neuer, kleinerer Lauf mit
  `--count 1` oder Restanzahl).

## 6. Schritt 4 — Zuweisen im Web

Kein Sondermodus am Terminal nötig — das passiert komplett im Browser:

1. Im Backend-Frontend als Admin/HR (Recht **`admin:cards`** – "NFC-Karten
   verwalten") auf die Seite **Einstellungen → Abschnitt „NFC-Karten"** gehen.
2. Dort erscheinen alle frisch personalisierten Fobs aus dem Pool mit Status
   **„Nicht zugewiesen"**.
3. Pro Karte: Mitarbeiter aus der Dropdown-Liste **„Mitarbeiter wählen …"**
   auswählen, dann **„Zuweisen"** klicken. Status springt auf **„Zugewiesen"**.
4. Fertig — der Mitarbeiter kann ab sofort mit diesem Fob stempeln.

## 7. Schritt 5 — Test-Stempelung

1. Einen bereits zugewiesenen Fob ans Terminal (PICC-Seite) halten.
2. Erwartet: Terminal zeigt den Namen des Mitarbeiters und bestätigt die
   Stempelung (Kommen/Gehen/Pause, je nach aktuellem Status).
3. **Test mit einem unbekannten/fremden Chip:** Eine beliebige andere
   Karte (Kreditkarte, ÖPNV-Karte, Handy mit NFC) an den Leser halten.
   Erwartet: **keine Reaktion außer Ablehnung** — kein Name, keine
   Stempelung. Das ist gewolltes Verhalten ("fail-closed"): Das Terminal
   akzeptiert grundsätzlich **nur** Karten, die die kryptografische Prüfung
   über den SAM bestehen. Es gibt keinen Rückfall auf "Kartennummer trotzdem
   akzeptieren".

## 8. Erst-Validierung (Pilot — wichtig)

Diese Krypto lief bisher nur gegen Software-Attrappen (Unit-Tests). Der
heutige Termin ist der **erste Test gegen echte Chips**. Gehen Sie deshalb
in dieser Reihenfolge vor — jeder Schritt schottet einen möglichen
Fehlerbereich ab, sodass ein Problem schnell eingegrenzt werden kann:

| # | Befehl | Prüft was | Braucht Hardware? |
|---|---|---|---|
| a | `sam_ceremony self-test` | Krypto-/Software-Pipeline der Ceremony (Schlüssel-Aufteilung/-Zusammensetzung) | Nein — läuft nur gegen eine Attrappe im Speicher |
| b | `sam_ceremony key-company` (mit echtem SAM, ohne `--backup-dir` als Trockenlauf möglich) | Host-Authentifizierung + Schlüssel-Laden auf **echter** SAM-Hardware | Ja, nur SAM — **kein Fob, keine zweite Person nötig**, nur SSH |
| c | `sam_personalize run --count 1` | Personalisierung eines **einzelnen** Fobs (EV2-Auth + Kartenschreiben) | Ja, SAM + 1 Fob |
| d | Test-Stempelung (Abschnitt 7) | EV2-Kartenauthentifizierung im laufenden Terminal-Betrieb | Ja, SAM + personalisierter Fob |

**Bei jedem Schritt gilt:**
- **Erfolg** = die im jeweiligen Abschnitt oben genannte Erfolgsmeldung
  erscheint (`self-test PASSED`, `OK`, `9000`, `summary: N/N ... OK`, Name +
  Stempelung am Terminal).
- **Fehler** = Programm bricht mit `FAILED: ...` ab, oder das Terminal
  reagiert nicht/anders als erwartet.
- **Bei jedem Fehler auf einer echten SAM/Karte (Schritt b–d): sofort
  stoppen, NICHT wiederholt neu versuchen**, sondern:
  1. Die komplette Bildschirmausgabe (stdout **und** stderr) kopieren.
  2. An das Entwickler-Team melden — Stichwort **„Hardware-Verify-Checkliste"**
     (im Code als `HARDWARE-VERIFY`-Kommentare markiert: mehrere
     Annahmen über das exakte SAM/DESFire-Byteformat sind bisher nur gegen
     Software-Attrappen geprüft, nicht gegen echte Chips — ein Fehler hier
     ist erwartbar und hilft, genau diese Annahmen zu bestätigen oder zu
     korrigieren).
  3. Erst nach Rückmeldung vom Team weitermachen.
- Schritt (a) muss **vor** jedem Vor-Ort-Termin grün sein — schlägt schon
  der Self-Test fehl, liegt der Fehler in der Software, nicht in der
  Hardware vor Ort, und der Termin sollte verschoben werden.

## 9. Fehlerbilder & Troubleshooting

| Symptom | Wahrscheinliche Ursache | Erster Schritt |
|---|---|---|
| `pcsc_scan` zeigt keinen zweiten Leser (kein SAM sichtbar) | SAM nicht (richtig) eingesteckt, oder Leser nicht neu erkannt | SAM-Sitz prüfen (Abschnitt 3), Kartenleser einmal aus-/wieder einstecken, `pcsc_scan` erneut |
| `sam_ceremony`/`sam_personalize`: `no PC/SC reader name contains "SAM"` (bzw. `"PICC"`) | Der Lesername enthält den erwarteten Textausschnitt nicht (anderes Lesermodell/andere Firmware-Bezeichnung) | `pcsc_scan` laufen lassen, tatsächlichen Namen ablesen, `--sam`/`--picc` (bzw. `KIOSK_SAM_READER`/`KIOSK_PICC_READER`) auf den echten Namensteil anpassen |
| Fob wird beim Stempeln abgelehnt, obwohl im Web als „Zugewiesen" markiert | `KIOSK_SAM_AID`/`KIOSK_SAM_KEY_ENTRY_NO`/`KIOSK_SAM_APP_KEY_NO`/`KIOSK_SAM_FILE_NO` auf dem Terminal stimmen nicht mit den bei der Personalisierung verwendeten `--aid`/`--company-key-entry-no`/`--app-key-no`/`--file-no` überein | Werte in `~/zeit-kiosk/run-kiosk.sh` mit den Befehlen aus Schritt 2/3 vergleichen |
| Fob wurde nie erfolgreich personalisiert (kein Eintrag im Pool) | Tap zu kurz, Fob war nicht wirklich leer/schon personalisiert, oder Batch ist mit Fehler abgebrochen | In der `sam_personalize`-Ausgabe nach `fob N: FAILED: ...` suchen, Fob einzeln mit `--count 1` erneut versuchen |
| `sam_personalize`/`sam_ceremony`: Fehler beim Backend-Aufruf (`register-to-pool`/HTTP) | Backend nicht erreichbar, falsche `KIOSK_BACKEND_URL`/`KIOSK_TENANT_ID`/`KIOSK_DEVICE_KEY` | Mit `curl <KIOSK_BACKEND_URL>/api/v1/health` (o. ä.) prüfen, ob der Server vom Pi aus erreichbar ist; Variablen mit `run-kiosk.sh` abgleichen |
| Terminal startet gar nicht / stürzt beim Start ab | Fehlende Pflicht-Variable (`KIOSK_SAM_AID`/`KIOSK_SAM_KEY_ENTRY_NO`) bei `KIOSK_READER=pcsc` | Fehlermeldung lesen (`required environment variable ... is not set`), fehlende Zeile in `run-kiosk.sh` ergänzen |
| Terminal läuft noch mit altem Programm, obwohl neu gebaut wurde | Alter Prozess läuft noch weiter (Respawn-Loop) | `pkill -f run-kiosk.sh` dann `killall zeiterfassung-kiosk`, danach Terminal neu starten (`~/zeit-kiosk/run-kiosk.sh &` bzw. über die reguläre Autostart-Kette) |
| Falscher Standort/falsches Terminal am falschen Ort im Einsatz | Terminal-Registrierung (Device-Key/Tenant) verwechselt | Mit dem Team abklären, welches Gerät für diesen Standort registriert ist, **bevor** weiter personalisiert wird |

---

Fragen oder unerwartetes Verhalten außerhalb dieser Tabelle: **nicht raten,
Team fragen** — gerade beim ersten Hardware-Lauf ist jede Auffälligkeit
relevant.
