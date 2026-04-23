"""Pydantic models for the Pulse-Check API request/response schemas."""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class MonitorStatus(str, Enum):
    """Possible states a monitor can be in."""

    ACTIVE = "active"
    PAUSED = "paused"
    DOWN = "down"


# ── Request Models ──────────────────────────────────────────────────────────


class MonitorCreate(BaseModel):
    """Schema for creating a new monitor."""

    id: str = Field(..., description="Unique identifier for the device", examples=["device-123"])
    timeout: int = Field(
        ..., gt=0, description="Countdown duration in seconds", examples=[60]
    )
    alert_email: str = Field(
        ..., description="Email address to notify on failure", examples=["admin@critmon.com"]
    )


# ── Response Models ─────────────────────────────────────────────────────────


class MessageResponse(BaseModel):
    """Generic JSON message response."""

    message: str


class MonitorResponse(BaseModel):
    """Detailed monitor state returned by status endpoints."""

    id: str
    timeout: int
    alert_email: str
    status: MonitorStatus
    created_at: datetime
    last_heartbeat: Optional[datetime] = None
    time_remaining: Optional[float] = Field(
        None, description="Seconds left on the countdown timer"
    )
