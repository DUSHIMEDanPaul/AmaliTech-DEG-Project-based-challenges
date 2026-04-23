"""
Monitor Manager – handles the lifecycle of device monitors.

Each monitor owns an asyncio timer task.  When the timer expires without
receiving a heartbeat the manager fires an alert and marks the device as
``down``.
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

logger = logging.getLogger("pulse-check")


class Monitor:
    """In-memory representation of a single device monitor."""

    def __init__(self, monitor_id: str, timeout: int, alert_email: str) -> None:
        self.id: str = monitor_id
        self.timeout: int = timeout
        self.alert_email: str = alert_email
        self.status: str = "active"
        self.created_at: datetime = datetime.now(timezone.utc)
        self.last_heartbeat: Optional[datetime] = None

        # Internal timer bookkeeping
        self._timer_task: Optional[asyncio.Task] = None
        self._timer_start: Optional[float] = None  # monotonic timestamp
        self._remaining_time: Optional[float] = None  # used when paused

    # ── Computed helpers ────────────────────────────────────────────────

    @property
    def time_remaining(self) -> Optional[float]:
        """Return seconds left on the countdown (real-time calculation)."""
        if self.status == "down":
            return 0.0
        if self.status == "paused":
            return round(self._remaining_time, 2) if self._remaining_time else 0.0
        if self._timer_start is not None:
            elapsed = time.monotonic() - self._timer_start
            remaining = (
                self._remaining_time if self._remaining_time else self.timeout
            ) - elapsed
            return max(0.0, round(remaining, 2))
        return None


class MonitorManager:
    """Manages all monitors and their countdown timers."""

    def __init__(self) -> None:
        self._monitors: Dict[str, Monitor] = {}

    # ── Queries ─────────────────────────────────────────────────────────

    def get(self, monitor_id: str) -> Optional[Monitor]:
        return self._monitors.get(monitor_id)

    def list_all(self) -> List[Monitor]:
        return list(self._monitors.values())

    # ── Commands ────────────────────────────────────────────────────────

    async def create(self, monitor_id: str, timeout: int, alert_email: str) -> Monitor:
        """Create (or replace) a monitor and start its countdown."""
        # If a monitor with this ID already exists, cancel its timer first
        if monitor_id in self._monitors:
            existing = self._monitors[monitor_id]
            if existing._timer_task and not existing._timer_task.done():
                existing._timer_task.cancel()

        monitor = Monitor(monitor_id, timeout, alert_email)
        self._monitors[monitor_id] = monitor
        self._start_timer(monitor, timeout)
        logger.info("Monitor '%s' created with %ds timeout.", monitor_id, timeout)
        return monitor

    async def heartbeat(self, monitor_id: str) -> Optional[Monitor]:
        """Reset the countdown for a monitor (also un-pauses if paused)."""
        monitor = self._monitors.get(monitor_id)
        if monitor is None:
            return None

        # Cancel existing timer
        if monitor._timer_task and not monitor._timer_task.done():
            monitor._timer_task.cancel()

        monitor.status = "active"
        monitor.last_heartbeat = datetime.now(timezone.utc)
        monitor._remaining_time = None
        self._start_timer(monitor, monitor.timeout)
        logger.info("Heartbeat received for '%s'. Timer reset to %ds.", monitor_id, monitor.timeout)
        return monitor

    async def pause(self, monitor_id: str) -> Optional[Monitor]:
        """Pause an active monitor's countdown."""
        monitor = self._monitors.get(monitor_id)
        if monitor is None:
            return None

        if monitor.status != "active":
            return monitor  # Only active monitors can be paused

        # Snapshot remaining time
        if monitor._timer_start is not None:
            elapsed = time.monotonic() - monitor._timer_start
            effective_timeout = monitor._remaining_time if monitor._remaining_time else monitor.timeout
            monitor._remaining_time = max(0.0, effective_timeout - elapsed)

        # Cancel the running timer
        if monitor._timer_task and not monitor._timer_task.done():
            monitor._timer_task.cancel()
            monitor._timer_task = None

        monitor._timer_start = None
        monitor.status = "paused"
        logger.info("Monitor '%s' paused with %.1fs remaining.", monitor_id, monitor._remaining_time or 0)
        return monitor

    async def delete(self, monitor_id: str) -> bool:
        """Remove a monitor entirely."""
        monitor = self._monitors.get(monitor_id)
        if monitor is None:
            return False

        if monitor._timer_task and not monitor._timer_task.done():
            monitor._timer_task.cancel()

        del self._monitors[monitor_id]
        logger.info("Monitor '%s' deleted.", monitor_id)
        return True

    async def cleanup(self) -> None:
        """Cancel all running timer tasks (called on shutdown)."""
        for monitor in self._monitors.values():
            if monitor._timer_task and not monitor._timer_task.done():
                monitor._timer_task.cancel()
        self._monitors.clear()

    # ── Internal helpers ────────────────────────────────────────────────

    def _start_timer(self, monitor: Monitor, duration: float) -> None:
        monitor._timer_start = time.monotonic()
        monitor._remaining_time = None
        monitor._timer_task = asyncio.create_task(
            self._countdown(monitor, duration)
        )

    async def _countdown(self, monitor: Monitor, duration: float) -> None:
        """Sleep for *duration* seconds, then fire the alert."""
        try:
            await asyncio.sleep(duration)

            # ── Timer expired – fire alert ──────────────────────────────
            monitor.status = "down"
            alert_payload = {
                "ALERT": f"Device {monitor.id} is down!",
                "time": datetime.now(timezone.utc).isoformat(),
                "alert_email": monitor.alert_email,
            }
            # Console output as required by the spec
            print(json.dumps(alert_payload))
            logger.critical(json.dumps(alert_payload))

        except asyncio.CancelledError:
            # Timer was cancelled (heartbeat / pause / delete) – expected
            pass
