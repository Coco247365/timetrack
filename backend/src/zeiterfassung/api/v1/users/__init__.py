"""User-domain router package (zeiterfassung-6pcv God-router split).

Former monolithic ``api/v1/users.py`` split into domain sub-routers, all
aggregated here into a single ``router`` with the exact same mounted paths
(this package is included with NO prefix in ``api/v1/router.py``, same as the
original module was).

Sub-modules:
- ``auth``: login/refresh/logout/change-password
- ``core``: user CRUD, own profile, DSGVO export, /auth/me
- ``org``: departments/companies/roles/accounts/scanned-cards/org-chart
- ``payroll``: manuelle Lohnbuchungen
- ``overtime``: Überstunden-Konto
"""

from __future__ import annotations

from fastapi import APIRouter

from zeiterfassung.api.v1.users import auth, core, org, overtime, payroll
from zeiterfassung.api.v1.users.auth import GENERIC_AUTH_ERROR  # re-exported for tests

router = APIRouter()

router.include_router(auth.router)
router.include_router(core.router)
router.include_router(org.router)
router.include_router(payroll.router)
router.include_router(overtime.router)

__all__ = ["router", "GENERIC_AUTH_ERROR"]
