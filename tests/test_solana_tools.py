"""
Tests for Solana blockchain tools: dedup fix and validators_info removal (Fixes #3, #4).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


SOLANA_CLUSTER_NODES_SAME_IP_DIFF_PORTS = {
    "result": [
        {
            "pubkey": "NodePubkey1",
            "gossip": "1.2.3.4:8001",
            "tpu": "1.2.3.4:8003",
            "rpc": "1.2.3.4:8899",
            "version": "1.18.0",
            "featureSet": 12345,
        }
    ]
}


@pytest.fixture
def solana_tools():
    from src.tools.blockchain.solana import SolanaTools
    return SolanaTools(rpc_url="https://test.solana.com", cache_ttl=3600)


# ============================================================
# Fix #3: Dedup by (ip, port) — same IP different ports both returned
# ============================================================


@pytest.mark.asyncio
async def test_get_seed_hosts_same_ip_different_ports_all_returned(solana_tools):
    """Same IP on gossip:8001, tpu:8003, rpc:8899 → three separate hosts."""
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = SOLANA_CLUSTER_NODES_SAME_IP_DIFF_PORTS

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        hosts = await solana_tools.get_seed_hosts("solana")

    assert len(hosts) == 3, "Same IP on different ports must produce separate hosts"
    ports = {h["port"] for h in hosts}
    assert 8001 in ports, "gossip port 8001 must be present"
    assert 8003 in ports, "tpu port 8003 must be present"
    assert 8899 in ports, "rpc port 8899 must be present"


@pytest.mark.asyncio
async def test_get_seed_hosts_same_ip_same_port_deduplicated(solana_tools):
    """Same IP AND same port → deduplicated to one host."""
    response = {
        "result": [
            {
                "pubkey": "NodePubkey1",
                "gossip": "1.2.3.4:8001",
                "tpu": "1.2.3.4:8001",  # same port as gossip
                "rpc": None,
                "version": "1.18.0",
            }
        ]
    }
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = response

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        hosts = await solana_tools.get_seed_hosts("solana")

    assert len(hosts) == 1, "Same IP+port must be deduplicated"


@pytest.mark.asyncio
async def test_get_seed_hosts_different_ips_different_ports_all_returned(solana_tools):
    """Different IPs produce separate hosts regardless of port."""
    response = {
        "result": [
            {
                "pubkey": "NodePubkey2",
                "gossip": "1.2.3.4:8001",
                "rpc": "5.6.7.8:8899",
                "tpu": None,
                "version": "1.18.0",
            }
        ]
    }
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = response

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        hosts = await solana_tools.get_seed_hosts("solana")

    assert len(hosts) == 2
    ips = {h["ip_address"] for h in hosts}
    assert "1.2.3.4" in ips
    assert "5.6.7.8" in ips


# ============================================================
# Fix #4: solana_get_validators_info removed
# ============================================================


def test_validators_info_not_in_schemas(solana_tools):
    """solana_get_validators_info must not appear in the schemas list."""
    schema_names = {s["function"]["name"] for s in solana_tools.schemas()}
    assert "solana_get_validators_info" not in schema_names


def test_validators_info_not_in_tool_map(solana_tools):
    """solana_get_validators_info must not appear in the tool map."""
    tool_map = solana_tools.get_tool_map()
    assert "solana_get_validators_info" not in tool_map


def test_cluster_nodes_and_vote_accounts_still_present(solana_tools):
    """cluster_nodes and vote_accounts must still be in schemas."""
    schema_names = {s["function"]["name"] for s in solana_tools.schemas()}
    assert "solana_get_cluster_nodes" in schema_names
    assert "solana_get_vote_accounts" in schema_names
