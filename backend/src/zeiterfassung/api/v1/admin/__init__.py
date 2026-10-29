"""Admin-domain router package (zeiterfassung-6pcv God-router split).

Former monolithic ``api/v1/admin.py`` split into domain sub-routers, all
aggregated here into a single ``router``. Mounted with ``prefix="/admin"`` by
``api/v1/router.py`` (same as the original module was) — sub-modules carry no
prefix of their own, so the final paths are unchanged.

Sub-modules:
- ``reports``: dashboard, approvals, monthly/yearly reports, audit log
- ``payroll_config``: wage types, surcharges, overtime rules, wage profiles
- ``settings``: feature toggles, sick-mail inboxes, work schedules, ICS feed
- ``work_time_profiles``: Arbeitszeit-Profile (Spec 2026-08-14)
"""

from __future__ import annotations

from fastapi import APIRouter

from zeiterfassung.api.v1.admin import payroll_config, reports, settings, work_time_profiles

router = APIRouter()

router.include_router(reports.router)
router.include_router(payroll_config.router)
router.include_router(settings.router)
router.include_router(work_time_profiles.router)
