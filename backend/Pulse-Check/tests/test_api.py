"""
Automated tests for the Pulse-Check API.

Run with:
    pytest tests/test_api.py -v
"""

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app, manager


@pytest.fixture(autouse=True)
async def _clean_manager():
    """Ensure each test starts with a fresh monitor store."""
    yield
    await manager.cleanup()


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ── Registration Tests ──────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_create_monitor(client: AsyncClient):
    """POST /monitors → 201 with confirmation message."""
    resp = await client.post(
        "/monitors",
        json={"id": "device-001", "timeout": 60, "alert_email": "admin@critmon.com"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert "device-001" in body["message"]
    assert "60s" in body["message"]


@pytest.mark.anyio
async def test_create_monitor_replaces_existing(client: AsyncClient):
    """Re-registering the same ID replaces the old monitor."""
    await client.post(
        "/monitors",
        json={"id": "device-dup", "timeout": 30, "alert_email": "a@b.com"},
    )
    resp = await client.post(
        "/monitors",
        json={"id": "device-dup", "timeout": 90, "alert_email": "new@b.com"},
    )
    assert resp.status_code == 201

    detail = await client.get("/monitors/device-dup")
    assert detail.json()["timeout"] == 90


# ── Heartbeat Tests ─────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_heartbeat_resets_timer(client: AsyncClient):
    """POST /monitors/{id}/heartbeat → 200 and resets countdown."""
    await client.post(
        "/monitors",
        json={"id": "device-hb", "timeout": 10, "alert_email": "a@b.com"},
    )

    resp = await client.post("/monitors/device-hb/heartbeat")
    assert resp.status_code == 200
    assert "reset" in resp.json()["message"].lower()

    # Verify monitor is still active
    detail = await client.get("/monitors/device-hb")
    assert detail.json()["status"] == "active"
    assert detail.json()["last_heartbeat"] is not None


@pytest.mark.anyio
async def test_heartbeat_not_found(client: AsyncClient):
    """POST /monitors/{id}/heartbeat → 404 for unknown ID."""
    resp = await client.post("/monitors/ghost-device/heartbeat")
    assert resp.status_code == 404


# ── Alert / Expiry Tests ───────────────────────────────────────────────────


@pytest.mark.anyio
async def test_timer_expiry_marks_device_down(client: AsyncClient):
    """When the countdown reaches 0 the monitor status becomes 'down'."""
    await client.post(
        "/monitors",
        json={"id": "device-exp", "timeout": 1, "alert_email": "ops@critmon.com"},
    )

    # Wait for the timer to expire
    await asyncio.sleep(1.5)

    detail = await client.get("/monitors/device-exp")
    assert detail.json()["status"] == "down"
    assert detail.json()["time_remaining"] == 0.0


@pytest.mark.anyio
async def test_heartbeat_on_expired_monitor_returns_409(client: AsyncClient):
    """Heartbeating a 'down' monitor should return 409."""
    await client.post(
        "/monitors",
        json={"id": "device-dead", "timeout": 1, "alert_email": "a@b.com"},
    )
    await asyncio.sleep(1.5)

    resp = await client.post("/monitors/device-dead/heartbeat")
    assert resp.status_code == 409


# ── Pause Tests ─────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_pause_stops_timer(client: AsyncClient):
    """POST /monitors/{id}/pause → 200 and timer stops."""
    await client.post(
        "/monitors",
        json={"id": "device-pause", "timeout": 5, "alert_email": "a@b.com"},
    )

    resp = await client.post("/monitors/device-pause/pause")
    assert resp.status_code == 200
    assert "paused" in resp.json()["message"].lower()

    detail = await client.get("/monitors/device-pause")
    assert detail.json()["status"] == "paused"


@pytest.mark.anyio
async def test_pause_prevents_alert(client: AsyncClient):
    """A paused monitor should NOT expire even after the original timeout."""
    await client.post(
        "/monitors",
        json={"id": "device-safe", "timeout": 1, "alert_email": "a@b.com"},
    )
    await client.post("/monitors/device-safe/pause")

    # Wait longer than the timeout
    await asyncio.sleep(1.5)

    detail = await client.get("/monitors/device-safe")
    assert detail.json()["status"] == "paused"  # NOT "down"


@pytest.mark.anyio
async def test_heartbeat_unpauses_monitor(client: AsyncClient):
    """Sending a heartbeat to a paused monitor un-pauses it."""
    await client.post(
        "/monitors",
        json={"id": "device-unpause", "timeout": 60, "alert_email": "a@b.com"},
    )
    await client.post("/monitors/device-unpause/pause")

    resp = await client.post("/monitors/device-unpause/heartbeat")
    assert resp.status_code == 200

    detail = await client.get("/monitors/device-unpause")
    assert detail.json()["status"] == "active"


@pytest.mark.anyio
async def test_pause_already_paused_returns_409(client: AsyncClient):
    """Pausing an already-paused monitor returns 409."""
    await client.post(
        "/monitors",
        json={"id": "device-pp", "timeout": 60, "alert_email": "a@b.com"},
    )
    await client.post("/monitors/device-pp/pause")

    resp = await client.post("/monitors/device-pp/pause")
    assert resp.status_code == 409


@pytest.mark.anyio
async def test_pause_not_found(client: AsyncClient):
    """Pausing a non-existent monitor returns 404."""
    resp = await client.post("/monitors/nope/pause")
    assert resp.status_code == 404


# ── Dashboard Tests (Developer's Choice) ───────────────────────────────────


@pytest.mark.anyio
async def test_list_monitors_empty(client: AsyncClient):
    """GET /monitors with no monitors returns an empty list."""
    resp = await client.get("/monitors")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.anyio
async def test_list_monitors_returns_all(client: AsyncClient):
    """GET /monitors returns all registered monitors."""
    await client.post(
        "/monitors",
        json={"id": "dev-a", "timeout": 60, "alert_email": "a@b.com"},
    )
    await client.post(
        "/monitors",
        json={"id": "dev-b", "timeout": 30, "alert_email": "b@b.com"},
    )

    resp = await client.get("/monitors")
    assert resp.status_code == 200
    ids = {m["id"] for m in resp.json()}
    assert ids == {"dev-a", "dev-b"}


@pytest.mark.anyio
async def test_get_monitor_detail(client: AsyncClient):
    """GET /monitors/{id} returns the monitor details."""
    await client.post(
        "/monitors",
        json={"id": "dev-detail", "timeout": 45, "alert_email": "x@y.com"},
    )

    resp = await client.get("/monitors/dev-detail")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "dev-detail"
    assert body["timeout"] == 45
    assert body["status"] == "active"
    assert body["time_remaining"] is not None


@pytest.mark.anyio
async def test_get_monitor_not_found(client: AsyncClient):
    """GET /monitors/{id} returns 404 for unknown ID."""
    resp = await client.get("/monitors/nope")
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_delete_monitor(client: AsyncClient):
    """DELETE /monitors/{id} removes the monitor."""
    await client.post(
        "/monitors",
        json={"id": "dev-del", "timeout": 60, "alert_email": "a@b.com"},
    )

    resp = await client.delete("/monitors/dev-del")
    assert resp.status_code == 200

    # Verify it's gone
    resp = await client.get("/monitors/dev-del")
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_delete_not_found(client: AsyncClient):
    """DELETE /monitors/{id} returns 404 for unknown ID."""
    resp = await client.delete("/monitors/nope")
    assert resp.status_code == 404


# ── Health Check ────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_health_check(client: AsyncClient):
    """GET /health returns 200."""
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"
