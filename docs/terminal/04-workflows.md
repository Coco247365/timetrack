# Workflows im Betrieb

> Teil der [Terminal-Serienproduktions-Doku](INDEX.md). Stand 2026-07-16,
> abgeleitet aus dem HW-Bringup 2026-07-15/16.

Drei wiederkehrende Abläufe: neue Fobs personalisieren (Enrollment), einen
Fob per Chip-UID einem Mitarbeiter zuweisen, und der tägliche Punch.

## 1. Fob-Enrollment am Terminal (Admin-Fob-gated)

Blanko-DESFire-Fobs werden **direkt am Wand-Terminal** personalisiert — kein
Laptop, kein SSH-Zugriff nötig. Der Zugang zu diesem Modus ist selbst über
einen **Admin-Fob** geschützt.

1. **Admin-Fob** ans Terminal halten → das Terminal erkennt ihn (Backend
   markiert diesen Fob als `is_admin`) und wechselt in den
   **Personalisierungs-Modus**.
2. Nacheinander die **Blanko-DESFire-Fobs** auflegen — je aufgelegter Fob:
   - wird programmiert (Anwendung anlegen → Schlüssel setzen →
     Token-Daten schreiben),
   - landet **sofort einzeln** im Karten-Pool (kein Sammeln bis zum Ende).
   - Der Zähler auf dem Bildschirm erhöht sich pro erfolgreichem Fob.
3. **Sequenzieller Batch:** immer ein Fob nach dem anderen abnehmen und den
   nächsten auflegen — das Terminal erkennt jede neue Auflage einzeln.
4. **Modus beenden** durch eines von:
   - Rotes ✕ / "Beenden"-Button auf dem Bildschirm,
   - denselben Admin-Fob erneut auflegen (Retap),
   - 5 Minuten ohne neue Auflage (automatischer Timeout).

> Der allererste Admin-Fob eines Terminals kann nicht über das Terminal
> selbst erzeugt werden (das wäre ein Henne-Ei-Problem — der Modus braucht
> ja bereits einen Admin-Fob). Er wird einmalig über das SSH-Provisioning-
> Werkzeug gemintet; alle weiteren Admin- **und** Mitarbeiter-Fobs laufen
> danach über den Terminal-Flow oben. Details dazu vom Team erfragen.

## 2. Handy-UID-Zuweisung

Ein frisch personalisierter, aber noch **nicht zugewiesener** Fob zeigt am
Terminal nur seine **Chip-UID** — nie den Token oder Schlüssel.

1. Den gepoolten, noch nicht zugewiesenen Fob ans Terminal halten.
2. Das Terminal zeigt die **Chip-UID** auf dem Bildschirm an. Im Hintergrund
   trägt das Terminal diese UID automatisch beim Backend-Eintrag nach
   (Pool-Backfill) — auch ohne, dass jemand aktiv etwas tut.
3. Ein Admin liest dieselbe UID **entweder**
   - direkt am Terminal-Bildschirm ab, **oder**
   - am Fob selbst per Handy-NFC-App (z. B. NXP TagInfo) — funktioniert,
     weil die UID bewusst nicht geheim ist (siehe
     [`01-ueberblick-architektur.md`](01-ueberblick-architektur.md)).
4. In der WebApp unter **"NFC-Karten verwalten"** den Fob mit genau dieser
   UID einem Mitarbeiter zuweisen.

## 3. Täglicher Punch

1. Mitarbeiter hält seinen zugewiesenen Fob ans Terminal.
2. Das Terminal führt die SAM-Authentifizierung durch (Challenge-Response,
   siehe [`01-ueberblick-architektur.md`](01-ueberblick-architektur.md)).
3. Bei erfolgreicher Prüfung zeigt das Kommen/Gehen/Pause-Menü auf dem
   Touchscreen; der Mitarbeiter bestätigt die passende Aktion.
4. Die Stempelung geht ans Backend (oder in die Offline-Warteschlange, falls
   das Netzwerk gerade nicht erreichbar ist).

Ein Fob, der die SAM-Prüfung nicht besteht (fremder/geklonter Chip), wird
**abgelehnt** — es gibt keinen Rückfall auf eine einfachere Prüfung.

## Implementierungsdetail (nur kurz)

Die SAM/DESFire-Kommunikation verwendet unterschiedliche Zählerkonventionen
zwischen SAM (big-endian) und Karte (little-endian); die EV2-Antwort-MAC
nutzt einen um 1 erhöhten Kommando-Zähler. Das ist HW-bestätigt und in der
Kiosk-Implementierung berücksichtigt — für die tägliche Nutzung ohne
Relevanz, nur als Hinweis für alle, die im Kiosk-Code selbst arbeiten.

## Weiterführend

- [`05-troubleshooting.md`](05-troubleshooting.md) — wenn ein Schritt hier nicht klappt
