#!/usr/bin/env bash
# Startet das Backend zuverlässig. Bewusst NICHT `uv run` — das re-synct und
# beschädigt auf diesem Rechner das Editable-Install (src verschwindet aus sys.path).
set -e
cd "$(dirname "$0")/.."
uv pip install -e . --no-deps -q
exec env PYTHONPATH=src .venv/bin/python -m uvicorn zeiterfassung.main:app \
  --host 127.0.0.1 --port 8000 "$@"
