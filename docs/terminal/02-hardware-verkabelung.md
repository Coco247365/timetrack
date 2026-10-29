# Hardware & Verkabelung

> Teil der [Terminal-Serienproduktions-Doku](INDEX.md). Stand 2026-07-16,
> abgeleitet aus dem HW-Bringup 2026-07-15/16. **Wichtigstes Dokument** dieser
> Serie — die Touch-Verkabelung hier ist der häufigste Stolperstein.

## Stückliste (BOM) pro Terminal

| Teil | Zweck |
|---|---|
| Raspberry Pi 5 | Terminal-Rechner |
| Waveshare 8DP-CAPLCD (8″ HDMI + USB-C-Touch) | Touchscreen für die Kiosk-UI |
| ACR1252U-M1 (Dual Reader) | NFC-Kartenleser mit SAM-Steckplatz |
| MF4SAM3 (SAM AV3) | Sicherheitschip (steckt im Reader) |
| Micro-HDMI → HDMI-Adapter/Kabel | Bildverbindung Panel → Pi |
| **Datengerechtes USB-C → USB-A-Kabel** | Touch-Verbindung Panel → Pi (siehe unten — **kein Charge-only-Kabel**) |
| Netzteil für das Panel | Stromversorgung des Panels (separat vom Pi-Netzteil) |
| Pi-5-Netzteil (offizielle USB-C-Stromversorgung) | Stromversorgung des Pi |
| Wandhalterung/Gehäuse | Montage |

## Die kritische Verkabelung — bitte genau lesen

> **⚠ Das Waveshare-Panel hat DREI GETRENNTE Buchsen: HDMI (Bild), Power
> (Strom) und eine DEDIZIERTE TOUCH-Buchse (USB-C). Alle drei müssen
> separat verkabelt werden — sie sind NICHT austauschbar.**
>
> - **Bild:** Panel-HDMI → Adapter → Pi-5-**Micro-HDMI**-Port.
> - **Strom:** Panel-Power-Buchse → eigenes Netzteil.
> - **Touch (der Stolperstein):** Panel-**TOUCH**-USB-C-Buchse → ein
>   **datenfähiges** USB-C→USB-A-Kabel → **Pi-5-USB-A-Port**.
>   - **NICHT** an den Pi-5-USB-C-Port anschließen — der ist am Pi 5 **nur
>     Strom**, kein Datenport.
>   - **NICHT** mit der Power-Buchse des Panels verwechseln.
>   - **KEIN Charge-only-Kabel** verwenden — solche Kabel übertragen nur
>     Strom (kein D+/D−), Touch-Daten kommen dann nie an.

Dieser eine Punkt (Touch-Buchse verwechselt oder falsches Kabel) war beim
HW-Bringup der Blocker, der am längsten unentdeckt blieb: die Kiosk-Software
lief einwandfrei, aber kein einziger Touch auf dem Bildschirm hatte eine
Wirkung.

## Verifikation: Touch wird erkannt

Nach dem Anschließen per SSH auf dem Pi prüfen:

```bash
# 1. USB-Gerät sichtbar?
lsusb
# erwartet u. a.: Bus 00X Device 00Y: ID 0712:000a Waveshare Waveshare
```

```bash
# 2. Als Input-Gerät registriert?
grep Name /proc/bus/input/devices
# erwartet: ein Eintrag mit "Waveshare" im Namen
```

```bash
# 3. libinput erkennt Touch-Fähigkeit?
sudo libinput list-devices
# erwartet: ein Gerät mit Waveshare-Namen und Capabilities: ... touch
```

**Symptom wenn falsch verkabelt:**
- Kein zusätzliches Touch-Gerät in `lsusb`/`/proc/bus/input/devices` (nur
  Reader + HDMI-Ausgabe sichtbar).
- **Kein** `dmesg`-Ereignis beim Ein-/Ausstecken des Touch-Kabels
  (`dmesg -w` währenddessen laufen lassen — bei korrekter Verkabelung
  erscheint sofort eine USB-Enumerationszeile).
- Die Kiosk-Buttons auf dem Bildschirm reagieren **nicht** auf Fingertipp
  — eine angeschlossene Maus funktioniert dagegen normal (das bestätigt: die
  App-Logik ist in Ordnung, es fehlt reine Touch-Eingabe).

## Stromlimit am Pi-5-USB-A-Port

Die Pi-5-USB-A-Ports haben standardmäßig ein Strom-Limit von 600 mA. Meldet
das System beim Touch-Anschluss "over-current" (z. B. in `dmesg`), in
`/boot/firmware/config.txt` ergänzen:

```ini
usb_max_current_enable=1
```

**Nur mit einem ordentlich dimensionierten Pi-Netzteil** aktivieren (offizielle
Pi-5-Stromversorgung, nicht ein schwaches Handy-Ladegerät) — sonst drohen
Unterspannungs-Probleme an anderer Stelle.

## Weiterführend

- [`03-provisioning.md`](03-provisioning.md) — Software-Setup nach der Verkabelung
- [`05-troubleshooting.md`](05-troubleshooting.md) — Touch-Fehlerbilder im Detail
