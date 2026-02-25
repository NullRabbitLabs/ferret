"""
Tests for blockchain tools: Sui and Solana.

Mocks httpx.AsyncClient.post to avoid real network calls.
"""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_asyncio


# ============================================================
# Sui Tools
# ============================================================

SUI_VALIDATORS_RESPONSE = {
    "result": {
        "activeValidators": [
            {
                "suiAddress": "0xabc",
                "name": "TestValidator",
                "netAddress": "/ip4/1.2.3.4/tcp/8080",
                "p2pAddress": "/ip4/1.2.3.4/tcp/8084",
                "primaryAddress": "/ip4/1.2.3.4/tcp/8081",
                "workerAddresses": ["/ip4/1.2.3.4/tcp/8082"],
                "votingPower": 100,
                "gasPrice": 1000,
                "commissionRate": 200,
                "nextEpochStake": 5000000,
                "stakingPoolActivationEpoch": 1,
            }
        ]
    }
}

SUI_COMMITTEE_RESPONSE = {
    "result": {
        "epoch": 42,
        "validators": [
            ["0xabc", 100],
            ["0xdef", 200],
        ],
    }
}


@pytest.fixture
def sui_tools():
    from src.tools.blockchain.sui import SuiTools
    return SuiTools(rpc_url="https://test.sui.io", cache_ttl=3600)


@pytest.mark.asyncio
async def test_sui_get_validators_calls_correct_rpc(sui_tools):
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = SUI_VALIDATORS_RESPONSE

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        result = await sui_tools.get_validators()

    call_args = mock_client.post.call_args
    payload = call_args[1]["json"]
    assert payload["method"] == "suix_getLatestSuiSystemState"
    assert result["count"] == 1
    assert result["validators"][0]["pubkey"] == "0xabc"
    assert result["validators"][0]["name"] == "TestValidator"


@pytest.mark.asyncio
async def test_sui_get_validators_parses_net_address(sui_tools):
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = SUI_VALIDATORS_RESPONSE

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        result = await sui_tools.get_validators()

    v = result["validators"][0]
    # Multiaddr should be parsed into host/port
    assert v["net_host"] == "1.2.3.4"
    assert v["net_port"] == 8080
    assert v["p2p_host"] == "1.2.3.4"
    assert v["p2p_port"] == 8084
    # Raw multiaddr and noisy fields should be absent
    assert "net_address" not in v
    assert "p2p_address" not in v
    assert "voting_power" not in v
    assert "gas_price" not in v
    assert "commission_rate" not in v


@pytest.mark.asyncio
async def test_sui_get_validators_caches_result(sui_tools):
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = SUI_VALIDATORS_RESPONSE

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        await sui_tools.get_validators()
        await sui_tools.get_validators()  # should use cache

    # Should only have made 1 HTTP request
    assert mock_client.post.call_count == 1


@pytest.mark.asyncio
async def test_sui_get_validators_cache_refreshes_after_ttl():
    from src.tools.blockchain.sui import SuiTools

    tools = SuiTools(rpc_url="https://test.sui.io", cache_ttl=0)  # TTL of 0 → always expired

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = SUI_VALIDATORS_RESPONSE

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        await tools.get_validators()
        await tools.get_validators()

    # With TTL=0 both calls should hit the network
    assert mock_client.post.call_count == 2


@pytest.mark.asyncio
async def test_sui_get_committee_parses_response(sui_tools):
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = SUI_COMMITTEE_RESPONSE

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        result = await sui_tools.get_committee()

    assert result["epoch"] == 42
    assert len(result["committee"]) == 2
    assert result["committee"][0] == {"pubkey": "0xabc", "stake": 100}


def test_sui_primary_tool_name(sui_tools):
    assert sui_tools.primary_tool_name() == "sui_get_validators"


@pytest.mark.asyncio
async def test_sui_get_seed_hosts_extracts_net_and_p2p(sui_tools):
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = SUI_VALIDATORS_RESPONSE

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        hosts = await sui_tools.get_seed_hosts("sui")

    # net_address and p2p_address are different ports in the fixture, so both should appear
    assert len(hosts) == 2
    rpc = next(h for h in hosts if h["service_type"] == "rpc")
    p2p = next(h for h in hosts if h["service_type"] == "p2p")
    assert rpc["ip_address"] == "1.2.3.4"
    assert rpc["port"] == 8080
    assert rpc["confidence"] == 0.95
    assert rpc["discovery_method"] == "on_chain"
    assert p2p["ip_address"] == "1.2.3.4"
    assert p2p["port"] == 8084


@pytest.mark.asyncio
async def test_sui_get_seed_hosts_deduplicates_same_ip_port(sui_tools):
    """If net_host and p2p_host are the same IP, only net is reported."""
    response = {
        "result": {
            "activeValidators": [
                {
                    "suiAddress": "0xabc",
                    "name": "SameIP",
                    "netAddress": "/ip4/1.2.3.4/tcp/8080",
                    "p2pAddress": "/ip4/1.2.3.4/tcp/8080",  # same as net
                }
            ]
        }
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

        hosts = await sui_tools.get_seed_hosts("sui")

    assert len(hosts) == 1  # deduplicated


@pytest.mark.asyncio
async def test_sui_get_committee_calls_correct_rpc(sui_tools):
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = SUI_COMMITTEE_RESPONSE

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        await sui_tools.get_committee(epoch=42)

    payload = mock_client.post.call_args[1]["json"]
    assert payload["method"] == "suix_getCommitteeInfo"
    assert payload["params"] == [42]


# ============================================================
# Solana Tools
# ============================================================

SOLANA_CLUSTER_NODES_RESPONSE = {
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

SOLANA_VOTE_ACCOUNTS_RESPONSE = {
    "result": {
        "current": [
            {
                "votePubkey": "VotePubkey1",
                "nodePubkey": "NodePubkey1",
                "activatedStake": 1000000,
                "commission": 10,
                "lastVote": 200000,
                "rootSlot": 199000,
            }
        ],
        "delinquent": [],
    }
}


@pytest.fixture
def solana_tools():
    from src.tools.blockchain.solana import SolanaTools
    return SolanaTools(rpc_url="https://test.solana.com", cache_ttl=3600)


def test_solana_primary_tool_name(solana_tools):
    assert solana_tools.primary_tool_name() == "solana_get_cluster_nodes"


@pytest.mark.asyncio
async def test_solana_get_seed_hosts_extracts_gossip_tpu_and_rpc(solana_tools):
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = SOLANA_CLUSTER_NODES_RESPONSE

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        hosts = await solana_tools.get_seed_hosts("solana")

    # gossip=1.2.3.4:8001, tpu=1.2.3.4:8003, rpc=1.2.3.4:8899
    # Same IP but different ports → all three hosts after Fix #3 (dedup by ip+port)
    assert len(hosts) == 3
    assert all(h["ip_address"] == "1.2.3.4" for h in hosts)
    ports = {h["port"] for h in hosts}
    assert 8001 in ports  # gossip
    assert 8003 in ports  # tpu
    assert 8899 in ports  # rpc
    assert all(h["confidence"] == 0.95 for h in hosts)


@pytest.mark.asyncio
async def test_solana_get_cluster_nodes_calls_correct_rpc(solana_tools):
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = SOLANA_CLUSTER_NODES_RESPONSE

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        result = await solana_tools.get_cluster_nodes()

    payload = mock_client.post.call_args[1]["json"]
    assert payload["method"] == "getClusterNodes"
    assert result["count"] == 1
    assert result["nodes"][0]["pubkey"] == "NodePubkey1"
    assert result["nodes"][0]["gossip"] == "1.2.3.4:8001"


@pytest.mark.asyncio
async def test_solana_get_cluster_nodes_caches(solana_tools):
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = SOLANA_CLUSTER_NODES_RESPONSE

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        await solana_tools.get_cluster_nodes()
        await solana_tools.get_cluster_nodes()  # cached

    assert mock_client.post.call_count == 1


@pytest.mark.asyncio
async def test_solana_get_vote_accounts_parses_response(solana_tools):
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = SOLANA_VOTE_ACCOUNTS_RESPONSE

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        result = await solana_tools.get_vote_accounts()

    assert len(result["current"]) == 1
    assert result["delinquent"] == []
    assert result["current"][0]["node_pubkey"] == "NodePubkey1"
    assert result["current"][0]["commission"] == 10
