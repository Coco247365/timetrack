#!/usr/bin/env bash
# dev-bootstrap.sh — Zeiterfassung lokal von 0 auf lauffähig (Mac-Dev).
#
# Ablauf: PostgreSQL prüfen -> Rolle + DB frisch anlegen -> backend/.env ->
#         uv sync -> DB-Inhalt (Dump bevorzugt, sonst Migrationen + seed_demo) ->
#         Login-Accounts sicherstellen -> frontend npm install.
#
# Konfiguration via ENV:
#   DUMP=/pfad/zur.dump   explizite Dump-Datei (custom format, pg_dump -Fc).
#                         Ohne Angabe: Auto-Suche in backups/*.dump und
#                         ~/Downloads/zeiterfassung.dump.
#   SEED=1                seed_demo erzwingen (ignoriert vorhandene Dumps).
#   DEV_PW=Test1234!      Passwort, das für alle Login-Accounts gesetzt wird.
#   START=1               Backend + Frontend am Ende im Hintergrund starten.
#   DB_NAME / DB_USER     Default: zeiterfassung / zeiterfassung.
#
# WARNUNG: legt die DB frisch an (drop + create). Nur für lokale Entwicklung.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BE="$ROOT/backend"
FE="$ROOT/frontend"
DB_NAME="${DB_NAME:-zeiterfassung}"
DB_USER="${DB_USER:-zeiterfassung}"
DEV_PW="${DEV_PW:-Test1234!}"
SEED="${SEED:-0}"
START="${START:-0}"
DUMP="${DUMP:-}"

# Python-Schritte (seed_demo, ensure_dev_accounts) lesen die DB-URL aus den
# Settings — hier auf die vom Skript aufgebaute DB festnageln, damit alles
# dieselbe DB trifft (auch bei abweichendem DB_NAME/DB_USER).
export ZEITERFASSUNG_DATABASE_URL="postgresql+asyncpg://$DB_USER:$DB_USER@127.0.0.1:5432/$DB_NAME"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$1"; }

say "PostgreSQL prüfen"
pg_isready -q || { echo "PostgreSQL läuft nicht → brew services start postgresql@17"; exit 1; }

say "Rolle '$DB_USER' sicherstellen"
psql -d postgres -tAc "SELECT 1 FROM pg_roles WHERE rolname='$DB_USER'" | grep -q 1 \
  || psql -d postgres -qc "CREATE ROLE \"$DB_USER\" LOGIN PASSWORD '$DB_USER'"

say "DB '$DB_NAME' frisch anlegen (drop+create; --force trennt offene Verbindungen)"
dropdb --if-exists --force "$DB_NAME"
createdb -O "$DB_USER" "$DB_NAME"

say "backend/.env sicherstellen"
if [ ! -f "$BE/.env" ]; then
  JWT="$(python3 -c 'import secrets; print(secrets.token_urlsafe(64))')"
  cat > "$BE/.env" <<EOF
ZEITERFASSUNG_DATABASE_URL=postgresql+asyncpg://$DB_USER:$DB_USER@127.0.0.1:5432/$DB_NAME
ZEITERFASSUNG_REDIS_URL=redis://localhost:6379/0
ZEITERFASSUNG_JWT_SECRET=$JWT
ZEITERFASSUNG_ENVIRONMENT=development
ZEITERFASSUNG_DEBUG=true
ZEITERFASSUNG_CORS_ORIGINS=["*"]
EOF
  echo "  backend/.env erzeugt (JWT generiert)"
else
  echo "  backend/.env existiert bereits — unverändert gelassen"
fi

say "Python-Deps (uv sync)"
( cd "$BE" && uv sync )

# Dump-Auto-Suche (nur wenn nicht SEED=1 und kein expliziter DUMP gesetzt)
if [ "$SEED" != "1" ] && [ -z "$DUMP" ]; then
  for c in "$ROOT"/backups/*.dump "$HOME/Downloads/zeiterfassung.dump"; do
    [ -f "$c" ] && DUMP="$c" && break
  done
fi

if [ "$SEED" != "1" ] && [ -n "$DUMP" ] && [ -f "$DUMP" ]; then
  say "DB-Inhalt aus Dump: $DUMP"
  pg_restore --no-owner --no-acl --role="$DB_USER" --exit-on-error -d "$DB_NAME" "$DUMP"
else
  say "DB-Inhalt via Migrationen + seed_demo"
  ( cd "$BE" && PYTHONPATH=src .venv/bin/alembic upgrade head )
  ( cd "$BE" && PYTHONPATH=src .venv/bin/python -m zeiterfassung.seed_demo )
fi

say "Login-Accounts sicherstellen (Dev-Passwort setzen)"
( cd "$BE" && PYTHONPATH=src .venv/bin/python scripts/ensure_dev_accounts.py "$DEV_PW" )

say "Frontend-Deps (npm install)"
[ -f "$FE/.env" ] || cp "$FE/.env.example" "$FE/.env"
( cd "$FE" && npm install --silent )

say "Fertig ✅"
cat <<EOF
Starten:
  Backend : cd backend && bash scripts/run.sh     # http://127.0.0.1:8000  (/docs · /health)
  Frontend: cd frontend && npm run dev            # http://localhost:3000
Login    : gf@example.com / $DEV_PW  (oder jeder andere Account aus 'accounts')
EOF

if [ "$START" = "1" ]; then
  say "Starte Backend + Frontend im Hintergrund"
  ( cd "$BE" && nohup bash scripts/run.sh > /tmp/zeit-backend.log 2>&1 & )
  ( cd "$FE" && nohup npm run dev > /tmp/zeit-frontend.log 2>&1 & )
  echo "  Logs: /tmp/zeit-backend.log · /tmp/zeit-frontend.log"
fi
