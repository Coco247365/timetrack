"""Hatchet worker entry point.

Run with::

    cd backend
    uv run python -m zeiterfassung.workflows.worker

Or as a dedicated process in docker-compose / systemd alongside the FastAPI app.
"""

from __future__ import annotations

from zeiterfassung.core.hatchet import get_hatchet
from zeiterfassung.workflows.approval_chain import approval_chain
from zeiterfassung.workflows.clock_processing import clock_processing
from zeiterfassung.workflows.daily_anomaly_check import daily_check


def main() -> None:
    hatchet = get_hatchet()
    worker = hatchet.worker(
        "zeiterfassung-worker",
        workflows=[clock_processing, approval_chain, daily_check],
    )
    worker.start()


if __name__ == "__main__":
    main()
