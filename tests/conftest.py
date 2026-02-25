"""
Shared test fixtures for Ferret.

Provides:
- mock_db: AsyncMock of DiscoveryApiClient
- mock_gateway: AsyncMock of DiscoveryGatewayClient
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from src.api_client import DiscoveryApiClient
from src.db import DiscoveryRun
from src.gateway_client import DiscoveryGatewayClient, GatewayResponse, ToolCall


@pytest.fixture
def mock_db():
    """Mock DiscoveryApiClient with sensible defaults."""
    db = AsyncMock(spec=DiscoveryApiClient)
    network_id = uuid4()
    run_id = uuid4()

    db.get_network_id.return_value = network_id
    db.get_network.return_value = {
        "id": network_id,
        "name": "sui",
        "chain_type": "sui",
        "rpc_endpoints": ["https://fullnode.mainnet.sui.io:443"],
        "enabled": True,
        "discovery_config": {"ports": [8080, 8081, 8082, 8083, 8084, 9184, 1337]},
    }
    db.create_discovery_run.return_value = DiscoveryRun(
        id=run_id,
        network_id=network_id,
        started_at=datetime.now(timezone.utc),
    )
    db.get_validators.return_value = []
    db.get_hosts.return_value = []
    db.get_recent_runs.return_value = []
    db.upsert_host.return_value = (uuid4(), True)
    db.flag_host_gone.return_value = True
    db.get_discovery_diff.return_value = {
        "new_hosts": [],
        "gone_hosts": [],
        "changed_hosts": [],
        "new_validators": [],
        "since": datetime.now(timezone.utc).isoformat(),
    }
    db.update_run_stats.return_value = None
    db.complete_discovery_run.return_value = None
    db.get_or_create_validator.return_value = uuid4()
    db.search_hypotheses.return_value = []
    db.save_hypothesis.return_value = uuid4()

    return db


@pytest.fixture
def mock_gateway():
    """Mock DiscoveryGatewayClient that returns a stop response by default."""
    gw = AsyncMock(spec=DiscoveryGatewayClient)
    gw.chat_with_tools.return_value = GatewayResponse(
        finish_reason="stop",
        tool_calls=[],
        text="Discovery complete.",
    )
    gw.get_embedding.return_value = [0.1] * 1536
    return gw


def make_tool_call_response(tool_name: str, args: dict, tc_id: str | None = None) -> GatewayResponse:
    """Helper to build a GatewayResponse containing a single tool call."""
    return GatewayResponse(
        finish_reason="tool_calls",
        tool_calls=[
            ToolCall(
                id=tc_id or f"call_{tool_name}",
                name=tool_name,
                arguments=args,
            )
        ],
        text=None,
        prompt_tokens=100,
        completion_tokens=50,
    )


def stop_response(text: str = "Done.") -> GatewayResponse:
    """Helper to build a stop response."""
    return GatewayResponse(finish_reason="stop", tool_calls=[], text=text)
