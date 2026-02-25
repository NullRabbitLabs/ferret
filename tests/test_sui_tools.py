"""
Tests for Sui blockchain tools: _parse_multiaddr, get_seed_hosts (Fixes #13, #15).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


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

        hosts = await sui_tools.get_seed_hosts("sui")

    for h in hosts:
        assert h["operator_name"] is None
