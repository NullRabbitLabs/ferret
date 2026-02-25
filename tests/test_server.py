"""Tests for ferret HTTP server."""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from src.db import DiscoveryRunResult
from src.server import app


@pytest.fixture
def run_result():
    return DiscoveryRunResult(
        run_id=uuid4(),
        network="sui",
        hosts_discovered=5,
        hosts_new=3,
        hosts_updated=2,
        hosts_gone=0,
        tool_calls=10,
        llm_tokens_used=1000,
        summary="Found 3 new hosts.",
    )


@pytest.fixture
def mock_db():
    db = AsyncMock()
    db.get_network.return_value = {
        "id": str(uuid4()),
        "name": "sui",
        "chain_type": "sui",
        "rpc_endpoints": ["https://fullnode.mainnet.sui.io:443"],
        "enabled": True,
    }
    return db


@pytest.fixture
def mock_gateway():
    return AsyncMock()


@pytest.fixture
def mock_state_tools():
    return MagicMock()


async def test_health():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_discover_success(mock_db, mock_gateway, mock_state_tools, run_result):
    mock_agent = MagicMock()
    mock_agent.run = AsyncMock(return_value=run_result)

    with (
        patch(
            "src.server._setup",
            new=AsyncMock(return_value=(mock_db, mock_gateway, mock_state_tools)),
        ),
        patch("src.server.DiscoveryAgent", return_value=mock_agent),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/discover", json={"network": "sui"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["network"] == "sui"
    assert data["hosts_new"] == 3
    assert data["hosts_updated"] == 2
    assert data["tool_calls"] == 10
    assert data["summary"] == "Found 3 new hosts."


async def test_discover_with_focus(mock_db, mock_gateway, mock_state_tools, run_result):
    mock_agent = MagicMock()
    mock_agent.run = AsyncMock(return_value=run_result)

    with (
        patch(
            "src.server._setup",
            new=AsyncMock(return_value=(mock_db, mock_gateway, mock_state_tools)),
        ),
        patch("src.server.DiscoveryAgent", return_value=mock_agent),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/discover", json={"network": "sui", "focus": "new validators only"}
            )

    assert resp.status_code == 200
    mock_agent.run.assert_called_once_with(network="sui", focus="new validators only")


async def test_discover_unknown_network(mock_db, mock_gateway, mock_state_tools):
    mock_db.get_network.return_value = None

    with patch(
        "src.server._setup",
        new=AsyncMock(return_value=(mock_db, mock_gateway, mock_state_tools)),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/discover", json={"network": "unknown-chain"})

    assert resp.status_code == 404
    assert "unknown-chain" in resp.json()["detail"]


async def test_discover_missing_network_field():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/discover", json={})
    assert resp.status_code == 422


async def test_discover_closes_clients_on_success(
    mock_db, mock_gateway, mock_state_tools, run_result
):
    mock_agent = MagicMock()
    mock_agent.run = AsyncMock(return_value=run_result)

    with (
        patch(
            "src.server._setup",
            new=AsyncMock(return_value=(mock_db, mock_gateway, mock_state_tools)),
        ),
        patch("src.server.DiscoveryAgent", return_value=mock_agent),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.post("/discover", json={"network": "sui"})

    mock_db.close.assert_called_once()
    mock_gateway.close.assert_called_once()
