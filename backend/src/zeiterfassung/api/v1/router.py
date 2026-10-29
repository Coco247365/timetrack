"""Top-level v1 API router — assembles all sub-routers."""

from fastapi import APIRouter

from zeiterfassung.api.v1 import absences, admin, br, clock, devices, export, holidays, users

api_router = APIRouter()

api_router.include_router(clock.router, prefix="/clock", tags=["clock"])
api_router.include_router(absences.router, prefix="/absences", tags=["absences"])
api_router.include_router(users.router, tags=["users"])
api_router.include_router(admin.router, prefix="/admin", tags=["admin"])
api_router.include_router(devices.router, prefix="/admin/devices", tags=["admin", "devices"])
api_router.include_router(export.router, prefix="/export", tags=["export"])
api_router.include_router(br.router)  # prefix="/br" is set on the router itself
api_router.include_router(holidays.router, tags=["holidays"])
