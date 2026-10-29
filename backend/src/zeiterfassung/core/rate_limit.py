"""In-process Sliding-Window-Rate-Limiting fuer die Auth-Endpunkte.

Gleiche Mechanik wie die NFC-Limits in api/v1/clock.py (bewusst
dependency-frei, kein slowapi/Redis): ein modulweiter Dict von Schluessel ->
Zeitstempel-Deque. Zustand ist pro Prozess — ein Mehrprozess-Deployment
braeuchte gemeinsamen Zustand (z. B. Redis); wie bei NFC ausserhalb des Scopes
(ein uvicorn-Prozess auf dem Server).

Die 429-Antwort ist generisch (kein Hinweis, WELCHER Schluessel — IP oder
E-Mail — ausgeloest hat) und traegt Retry-After.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import HTTPException, status

_buckets: dict[str, deque[float]] = defaultdict(deque)

# Ab dieser Gesamtzahl Schluessel werden abgelaufene Buckets (neuester Eintrag
# aelter als das Fenster) aufgeraeumt (Schutz gegen unbegrenztes Wachstum durch
# wechselnde IPs/E-Mails).
_PRUNE_THRESHOLD = 4096


def _now() -> float:
    """Indirektion fuer Tests (monkeypatch) — sonst time.monotonic."""
    return time.monotonic()


def reset() -> None:
    """Alle Buckets verwerfen (Test-Helper, autouse-Fixture in conftest)."""
    _buckets.clear()


def check(key: str, max_requests: int, window_seconds: float) -> None:
    """429, wenn *key* im Sliding-Window mehr als *max_requests* Treffer hat."""
    now = _now()
    cutoff = now - window_seconds
    bucket = _buckets[key]
    while bucket and bucket[0] < cutoff:
        bucket.popleft()
    if len(bucket) >= max_requests:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests — please slow down.",
            headers={"Retry-After": str(int(window_seconds))},
        )
    bucket.append(now)
    if len(_buckets) > _PRUNE_THRESHOLD:
        stale = [k for k, b in _buckets.items() if not b or b[-1] < cutoff]
        for k in stale:
            del _buckets[k]
