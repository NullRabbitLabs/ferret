"""Tests for ferret HTTP server."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from src.db import DiscoveryRun
from src.server import app

from datetime import datetime, timezone


def _make_run(run_id=None, network_name="sui"):
    return DiscoveryRun(
        id=run_id or uuid4(),
        network_name=network_name,
        started_at=datetime.now(timezone.utc),
        status="running",
    )


@pytest.fixture
def mock_api_client():
    client = AsyncMock()
    client.create_discovery_run.return_value = _make_run()
    client.close = AsyncMock()
    return client


async def test_health():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_discover_returns_run_id_immediately(mock_api_client):
    run = _make_run()
    mock_api_client.create_discovery_run.return_value = run

    with (
        patch("src.server.DiscoveryApiClient", return_value=mock_api_client),
        patch("src.server.asyncio.create_task") as mock_task,
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/discover", json={"network": "sui"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["run_id"] == str(run.id)
    assert data["status"] == "running"
    mock_task.assert_called_once()


async def test_discover_starts_background_task(mock_api_client):
    with (
        patch("src.server.DiscoveryApiClient", return_value=mock_api_client),
        patch("src.server.asyncio.create_task") as mock_task,
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.post("/discover", json={"network": "sui"})

    mock_task.assert_called_once()


async def test_discover_with_focus(mock_api_client):
    run = _make_run()
    mock_api_client.create_discovery_run.return_value = run

    captured_coro = None

    def capture_task(coro):
        nonlocal captured_coro
        captured_coro = coro
        return MagicMock()

    with (
        patch("src.server.DiscoveryApiClient", return_value=mock_api_client),
        patch("src.server.asyncio.create_task", side_effect=capture_task),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.post("/discover", json={"network": "sui", "focus": "new validators only"})

    # Clean up the coroutine to avoid resource warning
    if captured_coro is not None:
        captured_coro.close()


async def test_discover_unknown_network():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/discover", json={"network": "unknown-chain"})

    assert resp.status_code == 404
    assert "unknown-chain" in resp.json()["detail"]


async def test_discover_missing_network_field():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/discover", json={})
    assert resp.status_code == 422


async def test_discover_closes_client_after_response(mock_api_client):
    with (
        patch("src.server.DiscoveryApiClient", return_value=mock_api_client),
        patch("src.server.asyncio.create_task"),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.post("/discover", json={"network": "sui"})

    mock_api_client.close.assert_called_once()
