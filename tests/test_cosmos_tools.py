"""Tests for Cosmos Hub blockchain tools."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


CHAIN_REGISTRY_RESPONSE = {
    "peers": {
        "seeds": [
            {"id": "abc123", "address": "seeds.polkachu.com:14956", "provider": "Polkachu"},
            {"id": "def456", "address": "52.79.43.100:26656"},
        ],
        "persistent_peers": [
            {"id": "ghi789", "address": "65.108.195.29:36656", "provider": "Staketab"},
        ],
    }
}


@pytest.fixture
def cosmos_tools():
    from src.tools.blockchain.cosmos import CosmosTools
    return CosmosTools(cache_ttl=3600)


def _mock_http_get(json_data):
    """Build an AsyncMock httpx client that returns json_data on GET."""
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = json_data

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=mock_response)
    return mock_client


@pytest.mark.asyncio
async def test_get_seed_hosts_returns_hosts_from_chain_registry(cosmos_tools):
    """get_seed_hosts() returns all seeds + persistent_peers from chain registry."""
    with patch("httpx.AsyncClient", return_value=_mock_http_get(CHAIN_REGISTRY_RESPONSE)):
        hosts = await cosmos_tools.get_seed_hosts("cosmos")

    assert len(hosts) == 3


@pytest.mark.asyncio
async def test_get_seed_hosts_hostname_address_sets_hostname_field(cosmos_tools):
    """Hostname-based addresses set hostname field; ip_address is None."""
    with patch("httpx.AsyncClient", return_value=_mock_http_get(CHAIN_REGISTRY_RESPONSE)):
        hosts = await cosmos_tools.get_seed_hosts("cosmos")

    polkachu = next(h for h in hosts if h.get("hostname") == "seeds.polkachu.com")
    assert polkachu["hostname"] == "seeds.polkachu.com"
    assert polkachu["ip_address"] is None
    assert polkachu["port"] == 14956


@pytest.mark.asyncio
async def test_get_seed_hosts_ip_address_sets_ip_field(cosmos_tools):
    """IP-based addresses set ip_address field; hostname is None."""
    with patch("httpx.AsyncClient", return_value=_mock_http_get(CHAIN_REGISTRY_RESPONSE)):
        hosts = await cosmos_tools.get_seed_hosts("cosmos")

    ip_host = next(h for h in hosts if h.get("ip_address") == "52.79.43.100")
    assert ip_host["ip_address"] == "52.79.43.100"
    assert ip_host["hostname"] is None
    assert ip_host["port"] == 26656


@pytest.mark.asyncio
async def test_get_seed_hosts_returns_empty_on_http_error(cosmos_tools):
    """get_seed_hosts() returns [] and does not raise when HTTP call fails."""
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(side_effect=Exception("connection refused"))

    with patch("httpx.AsyncClient", return_value=mock_client):
        hosts = await cosmos_tools.get_seed_hosts("cosmos")

    assert hosts == []


@pytest.mark.asyncio
async def test_get_seed_hosts_caches_chain_registry_response(cosmos_tools):
    """HTTP is called exactly once; second get_seed_hosts call uses cached data."""
    with patch("httpx.AsyncClient", return_value=_mock_http_get(CHAIN_REGISTRY_RESPONSE)) as mock_cls:
        await cosmos_tools.get_seed_hosts("cosmos")
        await cosmos_tools.get_seed_hosts("cosmos")
        assert mock_cls.call_count == 1


@pytest.mark.asyncio
async def test_get_seed_hosts_bypasses_cache_when_expired(cosmos_tools):
    """Expired cache triggers a new HTTP call."""
    cosmos_tools._cache_ttl = 0
    with patch("httpx.AsyncClient", return_value=_mock_http_get(CHAIN_REGISTRY_RESPONSE)) as mock_cls:
        await cosmos_tools.get_seed_hosts("cosmos")
        await cosmos_tools.get_seed_hosts("cosmos")
        assert mock_cls.call_count == 2


@pytest.mark.asyncio
async def test_get_seed_hosts_deduplicates_same_host_and_port(cosmos_tools):
    """Two entries with the same address collapse to one host."""
    response = {
        "peers": {
            "seeds": [
                {"id": "aaa", "address": "52.79.43.100:26656"},
                {"id": "bbb", "address": "52.79.43.100:26656"},
            ],
            "persistent_peers": [],
        }
    }
    with patch("httpx.AsyncClient", return_value=_mock_http_get(response)):
        hosts = await cosmos_tools.get_seed_hosts("cosmos")

    assert len(hosts) == 1


@pytest.mark.asyncio
async def test_get_seed_hosts_empty_when_no_peers(cosmos_tools):
    """get_seed_hosts() returns [] when registry returns no seeds or persistent_peers."""
    response = {"peers": {"seeds": [], "persistent_peers": []}}
    with patch("httpx.AsyncClient", return_value=_mock_http_get(response)):
        hosts = await cosmos_tools.get_seed_hosts("cosmos")

    assert hosts == []


@pytest.mark.asyncio
async def test_get_seed_hosts_includes_node_id_as_validator_pubkey(cosmos_tools):
    """Node ID from registry entry is stored as validator_pubkey."""
    with patch("httpx.AsyncClient", return_value=_mock_http_get(CHAIN_REGISTRY_RESPONSE)):
        hosts = await cosmos_tools.get_seed_hosts("cosmos")

    polkachu = next(h for h in hosts if h.get("hostname") == "seeds.polkachu.com")
    assert polkachu["validator_pubkey"] == "abc123"


def test_parse_peer_address_hostname():
    from src.tools.blockchain.cosmos import _parse_peer_address
    hostname, ip, port = _parse_peer_address("seeds.polkachu.com:14956")
    assert hostname == "seeds.polkachu.com"
    assert ip is None
    assert port == 14956


def test_parse_peer_address_ip():
    from src.tools.blockchain.cosmos import _parse_peer_address
    hostname, ip, port = _parse_peer_address("52.79.43.100:26656")
    assert hostname is None
    assert ip == "52.79.43.100"
    assert port == 26656


def test_parse_peer_address_invalid_returns_none_tuple():
    from src.tools.blockchain.cosmos import _parse_peer_address
    hostname, ip, port = _parse_peer_address("bad")
    assert hostname is None
    assert ip is None
    assert port is None


def test_parse_peer_address_empty_returns_none_tuple():
    from src.tools.blockchain.cosmos import _parse_peer_address
    hostname, ip, port = _parse_peer_address("")
    assert hostname is None
    assert ip is None
    assert port is None
