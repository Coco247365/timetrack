"""Pydantic schemas for NFC device management."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class DeviceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    location: str | None = Field(default=None, max_length=255)


class DeviceResponse(BaseModel):
    """Response model for a device — never exposes key_hash or any key material."""

    model_config = {"from_attributes": True}

    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    location: str | None = None
    is_active: bool
    last_seen_at: datetime | None
    created_at: datetime


class DeviceRegisterResponse(DeviceResponse):
    """Returned once at registration — includes the plaintext key, never stored again."""

    key: str
