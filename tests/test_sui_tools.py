"""
Tests for Sui blockchain tools: _parse_multiaddr, get_seed_hosts, get_seed_peers,
enumerate_peers.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml


# ============================================================
# Fix #13: _parse_multiaddr — TCP and UDP/QUIC support
# ============================================================


def test_parse_multiaddr_tcp_ip4():
    from src.tools.blockchain.sui import _parse_multiaddr
    host, port = _parse_multiaddr("/ip4/1.2.3.4/tcp/8080")
    assert host == "1.2.3.4"
    assert port == 8080


def test_parse_multiaddr_tcp_with_suffix():
    from src.tools.blockchain.sui import _parse_multiaddr
    host, port = _parse_multiaddr("/ip4/1.2.3.4/tcp/8080/http")
    assert host == "1.2.3.4"
    assert port == 8080


def test_parse_multiaddr_tcp_ip6():
    from src.tools.blockchain.sui import _parse_multiaddr
    host, port = _parse_multiaddr("/ip6/::1/tcp/8080")
    assert host == "::1"
    assert port == 8080


def test_parse_multiaddr_tcp_dns4():
    from src.tools.blockchain.sui import _parse_multiaddr
    host, port = _parse_multiaddr("/dns4/validator.example.com/tcp/8080")
    assert host == "validator.example.com"
    assert port == 8080


def test_parse_multiaddr_tcp_dns():
    from src.tools.blockchain.sui import _parse_multiaddr
    host, port = _parse_multiaddr("/dns/validator.example.com/tcp/8080")
    assert host == "validator.example.com"
    assert port == 8080


def test_parse_multiaddr_udp_ip4():
    """UDP multiaddr (QUIC) must be parsed correctly."""
    from src.tools.blockchain.sui import _parse_multiaddr
    host, port = _parse_multiaddr("/ip4/1.2.3.4/udp/8080")
    assert host == "1.2.3.4"
    assert port == 8080


def test_parse_multiaddr_udp_with_quic():
    """UDP/QUIC format used by newer Sui validators."""
    from src.tools.blockchain.sui import _parse_multiaddr
    host, port = _parse_multiaddr("/ip4/1.2.3.4/udp/8084/quic-v1")
    assert host == "1.2.3.4"
    assert port == 8084


def test_parse_multiaddr_udp_dns4():
    from src.tools.blockchain.sui import _parse_multiaddr
    host, port = _parse_multiaddr("/dns4/validator.example.com/udp/9000")
    assert host == "validator.example.com"
    assert port == 9000


def test_parse_multiaddr_none_returns_none():
    from src.tools.blockchain.sui import _parse_multiaddr
    host, port = _parse_multiaddr(None)
    assert host is None
    assert port is None


def test_parse_multiaddr_empty_returns_none():
    from src.tools.blockchain.sui import _parse_multiaddr
    host, port = _parse_multiaddr("")
    assert host is None
    assert port is None


def test_parse_multiaddr_unrecognised_returns_none():
    from src.tools.blockchain.sui import _parse_multiaddr
    host, port = _parse_multiaddr("/onion/someaddress/8080")
    assert host is None
    assert port is None


# ============================================================
# Fix #15: get_seed_hosts includes operator_name
# ============================================================

SUI_VALIDATORS_WITH_NAME = {
    "result": {
        "activeValidators": [
            {
                "suiAddress": "0xabc",
                "name": "TestValidator",
                "netAddress": "/ip4/1.2.3.4/tcp/8080",
                "p2pAddress": "/ip4/1.2.3.4/tcp/8084",
            }
        ]
    }
}


@pytest.fixture
def sui_tools():
    from src.tools.blockchain.sui import SuiTools
    return SuiTools(rpc_url="https://test.sui.io", cache_ttl=3600)


@pytest.mark.asyncio
async def test_get_seed_hosts_includes_operator_name(sui_tools):
    """operator_name field must be populated from validator 'name'."""
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = SUI_VALIDATORS_WITH_NAME

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        with patch.object(sui_tools, "get_seed_peers", return_value=[]):
            hosts = await sui_tools.get_seed_hosts("sui")

    assert len(hosts) >= 1
    for h in hosts:
        assert "operator_name" in h, "operator_name must be present in seed host"
        assert h["operator_name"] == "TestValidator"


@pytest.mark.asyncio
async def test_get_seed_hosts_operator_name_none_when_no_name(sui_tools):
    """operator_name is None when validator has no 'name'."""
    response = {
        "result": {
            "activeValidators": [
                {
                    "suiAddress": "0xabc",
                    "netAddress": "/ip4/1.2.3.4/tcp/8080",
                    "p2pAddress": "/ip4/1.2.3.4/tcp/8084",
                    # no 'name' field
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

        with patch.object(sui_tools, "get_seed_peers", return_value=[]):
            hosts = await sui_tools.get_seed_hosts("sui")

    for h in hosts:
        assert h["operator_name"] is None


# ============================================================
# Seed peers from Sui GitHub config
# ============================================================

SEED_PEER_MDX = """
## Mainnet

```yaml
p2p-config:
  seed-peers:
    - address: /dns/seed-1.mainnet.sui.io/udp/8084
      peer-id: abc123
    - address: /ip4/10.0.0.1/udp/8084
      peer-id: def456
    - address: /dns4/fullnode.example.com/tcp/8080
      peer-id: ghi789
```
"""


@pytest.mark.asyncio
async def test_get_seed_peers_parses_mdx(sui_tools):
    """get_seed_peers fetches GitHub MDX and parses seed entries."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = SEED_PEER_MDX
    mock_response.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        peers = await sui_tools.get_seed_peers("sui")

    assert len(peers) == 3
    ips = [p.get("ip_address") for p in peers]
    hostnames = [p.get("hostname") for p in peers]
    assert "10.0.0.1" in ips
    assert "seed-1.mainnet.sui.io" in hostnames
    assert "fullnode.example.com" in hostnames
    for p in peers:
        assert p["discovery_method"] == "seed_peer"
        assert p["confidence"] == 0.80
        assert p["validator_pubkey"] is None


@pytest.mark.asyncio
async def test_get_seed_peers_falls_back_on_http_error(sui_tools):
    """get_seed_peers falls back to hardcoded peers on HTTP errors."""
    mock_response = MagicMock()
    mock_response.status_code = 404
    mock_response.raise_for_status = MagicMock(side_effect=Exception("404"))

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        peers = await sui_tools.get_seed_peers("sui")

    # Falls back to _FALLBACK_SEED_PEERS["sui"] (10 mainnet entries)
    assert len(peers) >= 10
    hostnames = [p.get("hostname") for p in peers]
    assert any("mainnet.sui.io" in (h or "") for h in hostnames)
    for p in peers:
        assert p["discovery_method"] == "seed_peer"


@pytest.mark.asyncio
async def test_get_seed_peers_skips_unparseable_addresses(sui_tools):
    """Entries with unparseable multiaddrs are silently skipped."""
    entries = [
        {"address": "/onion/hiddenservice/8084", "peer-id": "xxx"},
        {"address": "/ip4/10.0.0.1/udp/8084", "peer-id": "yyy"},
    ]
    hosts = sui_tools._parse_seed_peer_entries(entries, "sui")
    assert len(hosts) == 1
    assert hosts[0]["ip_address"] == "10.0.0.1"


def test_extract_seed_peers_from_mdx():
    """MDX parser extracts correct block by network marker."""
    from src.tools.blockchain.sui import _extract_seed_peers_from_mdx

    mdx = """
## Mainnet
```yaml
p2p-config:
  seed-peers:
    - address: /dns/mel-00.mainnet.sui.io/udp/8084
      peer-id: aaa
```

## Testnet
```yaml
p2p-config:
  seed-peers:
    - address: /dns/yto-tnt-ssfn-01.testnet.sui.io/udp/8084
      peer-id: bbb
```
"""
    mainnet = _extract_seed_peers_from_mdx(mdx, "sui")
    assert len(mainnet) == 1
    assert "mainnet" in mainnet[0]["address"]

    testnet = _extract_seed_peers_from_mdx(mdx, "sui-testnet")
    assert len(testnet) == 1
    assert "testnet" in testnet[0]["address"]

    devnet = _extract_seed_peers_from_mdx(mdx, "sui-devnet")
    assert devnet == []


# ============================================================
# get_seed_hosts merges validators + seed peers
# ============================================================


@pytest.mark.asyncio
async def test_get_seed_hosts_merges_validators_and_seed_peers(sui_tools):
    """get_seed_hosts returns both validator hosts and seed peers, deduplicated."""
    # Mock get_validators to return one validator at 1.2.3.4
    validator_response = MagicMock()
    validator_response.raise_for_status = MagicMock()
    validator_response.json.return_value = SUI_VALIDATORS_WITH_NAME

    # Patch get_seed_peers to return one overlap + one new peer
    fake_seed_peers = [
        {"ip_address": "1.2.3.4", "hostname": None, "port": 8084,
         "service_type": "p2p", "confidence": 0.80, "discovery_method": "seed_peer",
         "validator_pubkey": None, "operator_name": None, "reasoning": "test"},
        {"ip_address": "10.0.0.99", "hostname": None, "port": 8084,
         "service_type": "p2p", "confidence": 0.80, "discovery_method": "seed_peer",
         "validator_pubkey": None, "operator_name": None, "reasoning": "test"},
    ]

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=validator_response)
        mock_client_cls.return_value = mock_client

        with patch.object(sui_tools, "get_seed_peers", return_value=fake_seed_peers):
            hosts = await sui_tools.get_seed_hosts("sui")

    # Should have validator hosts + one new seed peer (1.2.3.4:8084 is deduplicated)
    ips = [(h.get("ip_address"), h.get("port")) for h in hosts]
    assert ("10.0.0.99", 8084) in ips
    methods = {h["discovery_method"] for h in hosts}
    assert "on_chain" in methods
    assert "seed_peer" in methods


# ============================================================
# Peer enumeration from Prometheus metrics
# ============================================================

PROMETHEUS_METRICS = """
# HELP network_peer_connected Number of connected peers
# TYPE network_peer_connected gauge
network_peer_connected{peer_id="abc123",address="/ip4/10.0.0.1/tcp/8080"} 1
network_peer_connected{peer_id="def456",address="/ip4/10.0.0.2/tcp/8080"} 1
network_peer_connected{peer_id="ghi789",address="/ip4/10.0.0.3/tcp/8080"} 0
network_peer_connected{peer_id="jkl012",address="/dns4/peer.example.com/tcp/8080"} 1
"""


@pytest.mark.asyncio
async def test_enumerate_peers_parses_prometheus_metrics(sui_tools):
    """enumerate_peers extracts connected peer IPs from Prometheus metrics."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = PROMETHEUS_METRICS
    mock_response.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        result = await sui_tools.enumerate_peers(
            metrics_url="http://localhost:9184/metrics"
        )

    # Only connected peers (value=1), excluding value=0
    peers = result["peers"]
    ips = [p.get("ip_address") or p.get("hostname") for p in peers]
    assert "10.0.0.1" in ips
    assert "10.0.0.2" in ips
    assert "10.0.0.3" not in ips  # not connected
    assert "peer.example.com" in ips


@pytest.mark.asyncio
async def test_enumerate_peers_returns_empty_on_error(sui_tools):
    """enumerate_peers returns empty list on connection/HTTP errors."""
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(side_effect=Exception("Connection refused"))
        mock_client_cls.return_value = mock_client

        result = await sui_tools.enumerate_peers(
            metrics_url="http://unreachable:9184/metrics"
        )

    assert result["peers"] == []


@pytest.mark.asyncio
async def test_enumerate_peers_in_tool_map(sui_tools):
    """sui_enumerate_peers should be registered in the tool map."""
    tool_map = sui_tools.get_tool_map()
    assert "sui_enumerate_peers" in tool_map


def test_enumerate_peers_in_schemas(sui_tools):
    """sui_enumerate_peers should appear in the tool schemas."""
    schema_names = [s["function"]["name"] for s in sui_tools.schemas()]
    assert "sui_enumerate_peers" in schema_names


def test_seeding_only_tools_excludes_enumerate_peers(sui_tools):
    """sui_enumerate_peers must NOT be in seeding_only_tools — it's for the LLM."""
    seeding = sui_tools.seeding_only_tools()
    assert "sui_get_validators" in seeding
    assert "sui_get_committee" in seeding
    assert "sui_enumerate_peers" not in seeding
