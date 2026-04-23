"""
Pulse-Check API  –  Dead Man's Switch Service
===============================================
A backend service that manages stateful countdown timers for remote device
monitoring.  If a device fails to send a heartbeat before its timer expires,
the system fires an alert.

Run with:
    uvicorn app.main:app --reload
"""

import logging
from contextlib import asynccontextmanager
from typing import List

from fastapi import FastAPI, HTTPException

from app.models import MessageResponse, MonitorCreate, MonitorResponse, MonitorStatus
from app.monitor_manager import Monitor, MonitorManager

# ── Logging setup ───────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger("pulse-check")

# ── Application-wide monitor manager (in-memory store) ─────────────────────

manager = MonitorManager()


# ── Lifespan (startup / shutdown) ──────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Pulse-Check API starting up …")
    yield
    logger.info("Pulse-Check API shutting down – cancelling all timers …")
    await manager.cleanup()


# ── FastAPI app ─────────────────────────────────────────────────────────────

app = FastAPI(
    title="Pulse-Check API",
    description=(
        "Dead Man's Switch API for **CritMon Servers Inc.**  "
        "Monitors remote devices and fires alerts when heartbeats stop arriving."
    ),
    version="1.0.0",
    lifespan=lifespan,
)


# ── Helper ──────────────────────────────────────────────────────────────────


def _to_response(monitor: Monitor) -> MonitorResponse:
    """Convert an internal Monitor object to the public response schema."""
    return MonitorResponse(
        id=monitor.id,
        timeout=monitor.timeout,
        alert_email=monitor.alert_email,
        status=MonitorStatus(monitor.status),
        created_at=monitor.created_at,
        last_heartbeat=monitor.last_heartbeat,
        time_remaining=monitor.time_remaining,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  CORE ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════


@app.post(
    "/monitors",
    status_code=201,
    response_model=MessageResponse,
    summary="Register a new monitor",
    tags=["Monitors"],
)
async def create_monitor(body: MonitorCreate):
    """
    Create a new device monitor and start its countdown timer.

    If a monitor with the same **id** already exists it will be replaced.
    """
    monitor = await manager.create(body.id, body.timeout, body.alert_email)
    return MessageResponse(
        message=f"Monitor for device '{monitor.id}' created with a {monitor.timeout}s timeout."
    )


@app.post(
    "/monitors/{monitor_id}/heartbeat",
    response_model=MessageResponse,
    summary="Send a heartbeat",
    tags=["Monitors"],
)
async def heartbeat(monitor_id: str):
    """
    Reset the countdown timer for a device.

    - If the monitor is **active** the timer restarts from the beginning.
    - If the monitor is **paused** it is automatically un-paused and the
      timer restarts.
    - If the monitor is **down** (timer already expired) a `409 Conflict`
      is returned – create a new monitor instead.
    """
    monitor = manager.get(monitor_id)
    if monitor is None:
        raise HTTPException(status_code=404, detail=f"Monitor '{monitor_id}' not found.")

    if monitor.status == "down":
        raise HTTPException(
            status_code=409,
            detail=f"Monitor '{monitor_id}' has already expired. Re-register the monitor.",
        )

    result = await manager.heartbeat(monitor_id)
    return MessageResponse(
        message=f"Heartbeat received. Timer for '{result.id}' reset to {result.timeout}s."
    )


# ═══════════════════════════════════════════════════════════════════════════
#  BONUS – PAUSE / UN-PAUSE ("Snooze Button")
# ═══════════════════════════════════════════════════════════════════════════


@app.post(
    "/monitors/{monitor_id}/pause",
    response_model=MessageResponse,
    summary="Pause a monitor",
    tags=["Monitors"],
)
async def pause_monitor(monitor_id: str):
    """
    Pause the countdown timer for a device.

    While paused no alerts will fire.  To un-pause, send a heartbeat via
    `POST /monitors/{id}/heartbeat`.
    """
    monitor = manager.get(monitor_id)
    if monitor is None:
        raise HTTPException(status_code=404, detail=f"Monitor '{monitor_id}' not found.")

    if monitor.status == "paused":
        raise HTTPException(
            status_code=409, detail=f"Monitor '{monitor_id}' is already paused."
        )

    if monitor.status == "down":
        raise HTTPException(
            status_code=409,
            detail=f"Monitor '{monitor_id}' has already expired and cannot be paused.",
        )

    await manager.pause(monitor_id)
    return MessageResponse(message=f"Monitor '{monitor_id}' is now paused.")


# ═══════════════════════════════════════════════════════════════════════════
#  DEVELOPER'S CHOICE – Fleet Status Dashboard
# ═══════════════════════════════════════════════════════════════════════════


@app.get(
    "/monitors",
    response_model=List[MonitorResponse],
    summary="List all monitors",
    tags=["Dashboard"],
)
async def list_monitors():
    """
    Return every registered monitor with its current status and the
    real-time seconds remaining on its countdown.

    **Developer's Choice feature** – gives operators a fleet-wide view of
    device health at a glance.
    """
    return [_to_response(m) for m in manager.list_all()]


@app.get(
    "/monitors/{monitor_id}",
    response_model=MonitorResponse,
    summary="Get monitor details",
    tags=["Dashboard"],
)
async def get_monitor(monitor_id: str):
    """Return the current state of a single monitor."""
    monitor = manager.get(monitor_id)
    if monitor is None:
        raise HTTPException(status_code=404, detail=f"Monitor '{monitor_id}' not found.")
    return _to_response(monitor)


@app.delete(
    "/monitors/{monitor_id}",
    response_model=MessageResponse,
    summary="Delete a monitor",
    tags=["Dashboard"],
)
async def delete_monitor(monitor_id: str):
    """
    Remove a monitor and cancel its timer.  The device will no longer
    be tracked.
    """
    deleted = await manager.delete(monitor_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Monitor '{monitor_id}' not found.")
    return MessageResponse(message=f"Monitor '{monitor_id}' has been deleted.")


# ═══════════════════════════════════════════════════════════════════════════
#  HEALTH CHECK
# ═══════════════════════════════════════════════════════════════════════════


@app.get("/health", tags=["System"], summary="Health check")
async def health_check():
    """Simple liveness probe."""
    return {"status": "healthy"}
