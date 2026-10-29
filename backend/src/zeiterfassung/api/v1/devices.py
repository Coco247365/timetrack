"""Admin endpoints for NFC terminal device management."""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from zeiterfassung.api.deps import AdminUser, DBSession, hash_device_key
from zeiterfassung.db.scoping import get_scoped, tenant_scoped
from zeiterfassung.models.device import Device
from zeiterfassung.schemas.device import DeviceCreate, DeviceRegisterResponse, DeviceResponse

router = APIRouter()


@router.post("", response_model=DeviceRegisterResponse, status_code=status.HTTP_201_CREATED)
async def register_device(
    body: DeviceCreate,
    db: DBSession,
    admin: AdminUser,
) -> DeviceRegisterResponse:
    """Register a new NFC terminal device for the caller's tenant.

    The plaintext API key is returned **once** in this response and is never
    stored or retrievable again.  Configure the kiosk with this key immediately.
    """
    if not admin.has_permission_namespace("admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin permission required",
        )

    plaintext_key = "zdk_" + secrets.token_urlsafe(32)
    now = datetime.now(tz=UTC)

    device = Device(
        tenant_id=admin.tenant_id,
        name=body.name,
        location=body.location,
        key_hash=hash_device_key(plaintext_key),
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    db.add(device)
    await db.flush()
    await db.commit()
    await db.refresh(device)

    return DeviceRegisterResponse(
        id=device.id,
        tenant_id=device.tenant_id,
        name=device.name,
        location=device.location,
        is_active=device.is_active,
        last_seen_at=device.last_seen_at,
        created_at=device.created_at,
        key=plaintext_key,
    )


@router.get("", response_model=list[DeviceResponse])
async def list_devices(
    db: DBSession,
    admin: AdminUser,
) -> list[DeviceResponse]:
    """List all NFC terminal devices registered for the caller's tenant.

    Never exposes key_hash or any key material.
    """
    if not admin.has_permission_namespace("admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin permission required",
        )

    result = await db.execute(
        tenant_scoped(select(Device), Device, admin.tenant_id).order_by(Device.created_at)
    )
    devices = result.scalars().all()
    return [DeviceResponse.model_validate(d) for d in devices]


@router.post("/{device_id}/revoke", response_model=DeviceResponse)
async def revoke_device(
    device_id: uuid.UUID,
    db: DBSession,
    admin: AdminUser,
) -> DeviceResponse:
    """Revoke an NFC terminal device so its key is no longer accepted.

    The device record is retained for audit purposes; only ``is_active`` is
    set to ``False``.  Returns 404 if the device does not belong to the
    caller's tenant.
    """
    if not admin.has_permission_namespace("admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin permission required",
        )

    device = await get_scoped(db, Device, admin.tenant_id, device_id)
    if device is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Device not found",
        )

    device.is_active = False
    await db.flush()
    await db.commit()
    await db.refresh(device)
    return DeviceResponse.model_validate(device)
