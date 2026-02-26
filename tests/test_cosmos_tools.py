"""Tests for Cosmos Hub blockchain tools."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


COSMOS_NET_INFO_RESPONSE = {
    "result": {
        "listening": True,
        "n_peers": "2",
        "peers": [
            {
                "node_info": {
                    "id": "abc123def456",
                    "listen_addr": "tcp://0.0.0.0:26656",
                    "moniker": "my-validator",
                },
                "remote_ip": "1.2.3.4",
            },
            {
                "node_info": {
                    "id": "def456abc789",
                    "listen_addr": "tcp://0.0.0.0:26656",
                    "moniker": "another-validator",
                },
                "remote_ip": "5.6.7.8",
            },
        ],
    }
}


@pytest.fixture
def cosmos_tools():
    from src.tools.blockchain.cosmos import CosmosTools
    return CosmosTools(rpc_url="https://test.cosmos.com", cache_ttl=3600)


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
async def test_get_seed_hosts_returns_host_dicts(cosmos_tools):
    """get_seed_hosts() returns correctly-shaped host dicts from net_info peers."""
    with patch("httpx.AsyncClient", return_value=_mock_http_get(COSMOS_NET_INFO_RESPONSE)):
        hosts = await cosmos_tools.get_seed_hosts("cosmos")

    assert len(hosts) == 2
    h = hosts[0]
    assert h["ip_address"] == "1.2.3.4"
    assert h["port"] == 26656
    assert h["service_type"] == "p2p"
    assert h["confidence"] == 0.9
    assert h["discovery_method"] == "on_chain"
    assert h["validator_pubkey"] == "abc123def456"


@pytest.mark.asyncio
async def test_get_seed_hosts_empty_when_no_peers(cosmos_tools):
    """get_seed_hosts() returns [] when net_info returns no peers."""
    with patch("httpx.AsyncClient", return_value=_mock_http_get({"result": {"peers": []}})):
        hosts = await cosmos_tools.get_seed_hosts("cosmos")
    assert hosts == []


@pytest.mark.asyncio
async def test_get_seed_hosts_returns_empty_on_rpc_error(cosmos_tools):
    """get_seed_hosts() returns [] and does not raise when RPC call fails."""
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(side_effect=Exception("connection refused"))

    with patch("httpx.AsyncClient", return_value=mock_client):
        hosts = await cosmos_tools.get_seed_hosts("cosmos")

    assert hosts == []


@pytest.mark.asyncio
async def test_get_seed_hosts_deduplicates_same_ip_and_port(cosmos_tools):
    """Two peers with the same remote_ip and port collapse to one host."""
    response = {
        "result": {
            "peers": [
                {
                    "node_info": {"id": "aaa", "listen_addr": "tcp://0.0.0.0:26656", "moniker": "a"},
                    "remote_ip": "1.2.3.4",
                },
                {
                    "node_info": {"id": "bbb", "listen_addr": "tcp://0.0.0.0:26656", "moniker": "b"},
                    "remote_ip": "1.2.3.4",
                },
            ]
        }
    }
    with patch("httpx.AsyncClient", return_value=_mock_http_get(response)):
        hosts = await cosmos_tools.get_seed_hosts("cosmos")

    assert len(hosts) == 1


@pytest.mark.asyncio
async def test_get_seed_hosts_skips_peers_without_remote_ip(cosmos_tools):
    """Peers with no remote_ip are skipped."""
    response = {
        "result": {
            "peers": [
                {
                    "node_info": {"id": "aaa", "listen_addr": "tcp://0.0.0.0:26656", "moniker": "a"},
                    "remote_ip": "",
                },
                {
                    "node_info": {"id": "bbb", "listen_addr": "tcp://0.0.0.0:26656", "moniker": "b"},
                    "remote_ip": "5.6.7.8",
                },
            ]
        }
    }
    with patch("httpx.AsyncClient", return_value=_mock_http_get(response)):
        hosts = await cosmos_tools.get_seed_hosts("cosmos")

    assert len(hosts) == 1
    assert hosts[0]["ip_address"] == "5.6.7.8"


@pytest.mark.asyncio
async def test_get_net_info_uses_cache_on_second_call(cosmos_tools):
    """HTTP is called exactly once; second call returns cached result."""
    with patch("httpx.AsyncClient", return_value=_mock_http_get(COSMOS_NET_INFO_RESPONSE)) as mock_cls:
        await cosmos_tools.get_net_info()
        await cosmos_tools.get_net_info()
        assert mock_cls.call_count == 1


@pytest.mark.asyncio
async def test_get_net_info_bypasses_cache_when_expired(cosmos_tools):
    """Expired cache triggers a new HTTP call."""
    cosmos_tools._cache_ttl = 0  # expire immediately
    with patch("httpx.AsyncClient", return_value=_mock_http_get(COSMOS_NET_INFO_RESPONSE)) as mock_cls:
        await cosmos_tools.get_net_info()
        await cosmos_tools.get_net_info()
        assert mock_cls.call_count == 2


def test_parse_port_from_listen_addr():
    from src.tools.blockchain.cosmos import _parse_port
    assert _parse_port("tcp://0.0.0.0:26656") == 26656
    assert _parse_port("tcp://1.2.3.4:26660") == 26660
    assert _parse_port("") is None
    assert _parse_port("no-port-here") is None
