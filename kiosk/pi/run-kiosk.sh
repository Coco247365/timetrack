#!/usr/bin/env bash
# Launch the Zeiterfassung kiosk inside the labwc/Wayland session.
# Deployed on the Pi at ~/zeit-kiosk/run-kiosk.sh (started from labwc autostart).
export XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-0
export SLINT_FULLSCREEN=1 SLINT_BACKEND=winit
export KIOSK_READER=pcsc
# Backend laeuft auf dem Server (im eigenen Netz/VPN) — auf dem Pi wird NICHTS gespeichert.
export KIOSK_BACKEND_URL=https://your-server
# Caddy-internes Zertifikat (nackte IP) akzeptieren — mit echter Domain entfernen.
export KIOSK_INSECURE_TLS=1
export KIOSK_TENANT_ID=<tenant-uuid>
# Offline-Puffer: hier landen Stempel, wenn der Server nicht erreichbar ist.
export KIOSK_QUEUE_PATH="$HOME/kiosk-offline-queue.jsonl"
export KIOSK_DEVICE_KEY=dev-terminal
export KIOSK_DEVICE_ID=PI-TERMINAL-01

# Host-spezifische + geheime Werte (echter KIOSK_DEVICE_KEY, KIOSK_SAM_AID,
# KIOSK_SAM_KEY_ENTRY_NO, …) liegen AUSSERHALB des git-Work-Trees in
# ~/kiosk.env (chmod 600): der Deploy-Hook macht `git checkout -f` und würde
# jede In-Place-Änderung an dieser Datei überschreiben. Die Werte oben sind
# nur Dev-Platzhalter; ~/kiosk.env gewinnt.
# shellcheck disable=SC1091
[ -f "$HOME/kiosk.env" ] && . "$HOME/kiosk.env"

# Respawn loop: the kiosk must always be on screen. If it ever exits (a crash, a
# stray close), relaunch it. Together with the labwc fullscreen window rule and
# the absence of any on-screen close affordance, this makes the terminal
# effectively un-closable for the user. (To stop it for maintenance, kill this
# script — `pkill -f run-kiosk.sh` — then `killall zeiterfassung-kiosk`.)
#
# Self-locating: launch the binary that ships in THIS checkout (this script lives
# at kiosk/pi/run-kiosk.sh, the binary at kiosk/kiosk/target/release/…), so a
# git-push deploy of the whole repo always runs its own freshly-built kiosk —
# no hard-coded per-clone path to drift. Override with $KIOSK_BIN if needed.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="${KIOSK_BIN:-$SCRIPT_DIR/../kiosk/target/release/zeiterfassung-kiosk}"
while true; do
  "$BIN"
  echo "[run-kiosk] kiosk exited (code $?), respawning in 1s…" >&2
  sleep 1
done
