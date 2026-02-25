"""
Shared test fixtures for the discovery agent.

Provides:
- mock_db: AsyncMock of Database
- mock_gateway: AsyncMock of DiscoveryGatewayClient
- real_db: asyncpg pool connected to nr_scan_test (requires DB)
"""

import asyncio
import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from src.db import Database, DiscoveryRun
from src.gateway_client import DiscoveryGatewayClient, GatewayResponse, ToolCall


# ============================================================
# Mock fixtures (no infrastructure required)
# ============================================================

@pytest.fixture
def mock_db():
    """Mock Database with sensible defaults."""
    db = AsyncMock(spec=Database)
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


# ============================================================
# Real database fixtures (requires nr_scan_test on port 5433)
# ============================================================

TEST_DB_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql://nr_scan:nr_scan_dev@localhost:5433/nr_scan_test",
)

MIGRATION_SQL = (
    open(
        os.path.join(
            os.path.dirname(__file__),
            "../../migrations/023_discovery_schema.sql",
        )
    )
    .read()
    .split("-- migrate:down")[0]
    .split("-- migrate:up")[1]
    .strip()
)


@pytest_asyncio.fixture(scope="session")
async def real_db():
    """
    asyncpg Database connected to nr_scan_test.

    Applies the discovery schema migration once per session.
    Requires the test database to exist.
    """
    db = Database(TEST_DB_URL, min_size=1, max_size=3)
    try:
        await db.connect()
    except Exception:
        pytest.skip("Test database not available at " + TEST_DB_URL)
        return

    # Apply migration (idempotent — uses IF NOT EXISTS)
    async with db.acquire() as conn:
        # Enable pgvector if available
        try:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        except Exception:
            pass
        for statement in MIGRATION_SQL.split(";"):
            stmt = statement.strip()
            if stmt:
                try:
                    await conn.execute(stmt)
                except Exception:
                    pass  # Some statements may already exist

    yield db
    await db.close()


@pytest_asyncio.fixture
async def clean_real_db(real_db):
    """Truncate discovery tables before each test."""
    async with real_db.acquire() as conn:
        await conn.execute(
            """
            TRUNCATE
                discovery.discovery_hypotheses,
                discovery.discovery_runs,
                discovery.hosts,
                discovery.validators
            RESTART IDENTITY CASCADE
            """
        )
    yield real_db
