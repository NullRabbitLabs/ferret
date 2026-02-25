"""
Tests for CDN/proxy IP detection and rejection (Fix #2).

_is_cdn_ip must reject Cloudflare, Fastly, and other CDN ranges.
report_discovered_host must reject CDN IPs before upsert.
"""

from uuid import uuid4

import pytest


def test_cloudflare_104_21_range_rejected():
    from src.tools.state import _is_cdn_ip
    assert _is_cdn_ip("104.21.0.1") is True
    assert _is_cdn_ip("104.21.128.5") is True
    assert _is_cdn_ip("104.21.255.255") is True


def test_cloudflare_172_67_range_rejected():
    from src.tools.state import _is_cdn_ip
    assert _is_cdn_ip("172.67.0.1") is True
    assert _is_cdn_ip("172.67.100.200") is True


def test_cloudflare_104_16_range_rejected():
    from src.tools.state import _is_cdn_ip
    assert _is_cdn_ip("104.16.0.1") is True
    assert _is_cdn_ip("104.16.100.1") is True


def test_fastly_151_101_range_rejected():
    from src.tools.state import _is_cdn_ip
    assert _is_cdn_ip("151.101.0.1") is True
    assert _is_cdn_ip("151.101.128.1") is True
    assert _is_cdn_ip("151.101.255.255") is True


def test_legitimate_aws_ip_accepted():
    from src.tools.state import _is_cdn_ip
    assert _is_cdn_ip("44.198.0.1") is False


def test_random_ips_accepted():
    from src.tools.state import _is_cdn_ip
    assert _is_cdn_ip("1.2.3.4") is False
    assert _is_cdn_ip("203.0.113.1") is False
    assert _is_cdn_ip("8.8.8.8") is False
    assert _is_cdn_ip("185.200.100.1") is False


def test_private_ips_not_cdn():
    """Private IPs are blocked by other mechanisms, but not flagged as CDN."""
    from src.tools.state import _is_cdn_ip
    assert _is_cdn_ip("10.0.0.1") is False
    assert _is_cdn_ip("192.168.1.1") is False


@pytest.fixture
def state_tools(mock_db):
    from src.tools.state import StateTools
    return StateTools(db=mock_db)


@pytest.mark.asyncio
async def test_report_discovered_host_rejects_cloudflare_ip(state_tools):
    result = await state_tools.report_discovered_host(
        network="sui",
        ip_address="104.21.10.1",
        service_type="rpc",
        confidence=0.9,
        discovery_method="ct_log",
        reasoning="Found in CT logs",
    )
    assert "error" in result
    assert "CDN" in result["error"] or "Rejected" in result["error"]


@pytest.mark.asyncio
async def test_report_discovered_host_rejects_fastly_ip(state_tools):
    result = await state_tools.report_discovered_host(
        network="sui",
        ip_address="151.101.1.2",
        service_type="rpc",
        confidence=0.9,
        discovery_method="ct_log",
        reasoning="Found in CT logs",
    )
    assert "error" in result
    assert "CDN" in result["error"] or "Rejected" in result["error"]


@pytest.mark.asyncio
async def test_report_discovered_host_rejects_172_67_ip(state_tools):
    result = await state_tools.report_discovered_host(
        network="sui",
        ip_address="172.67.50.100",
        service_type="rpc",
        confidence=0.9,
        discovery_method="ct_log",
        reasoning="Found in CT logs",
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_report_discovered_host_accepts_legitimate_ip(state_tools, mock_db):
    mock_db.upsert_host.return_value = (uuid4(), True)
    result = await state_tools.report_discovered_host(
        network="sui",
        ip_address="44.198.0.1",
        service_type="rpc",
        confidence=0.9,
        discovery_method="ct_log",
        reasoning="Found in CT logs",
    )
    assert "error" not in result
    assert result["ip_address"] == "44.198.0.1"


@pytest.mark.asyncio
async def test_report_discovered_host_cdn_rejection_does_not_call_db(state_tools, mock_db):
    """CDN rejection must happen before DB upsert."""

    await state_tools.report_discovered_host(
        network="sui",
        ip_address="104.21.10.1",
        service_type="rpc",
        confidence=0.9,
        discovery_method="ct_log",
        reasoning="Found in CT logs",
    )
    mock_db.upsert_host.assert_not_called()


@pytest.mark.asyncio
async def test_bulk_report_skips_cdn_ips(state_tools, mock_db):
    """bulk_report_discovered_hosts must skip CDN IPs, not store them."""
    mock_db.upsert_host.return_value = (uuid4(), True)

    result = await state_tools.bulk_report_discovered_hosts(
        network="sui",
        hosts=[
            {"ip_address": "104.21.10.1", "service_type": "rpc", "confidence": 0.9,
             "discovery_method": "on_chain"},   # Cloudflare — must be skipped
            {"ip_address": "1.2.3.4", "service_type": "rpc", "confidence": 0.9,
             "discovery_method": "on_chain"},   # legit — must be inserted
        ],
    )

    assert result["new"] == 1, "only the legit IP must be inserted"
    assert any("CDN" in e.get("error", "") for e in result["errors"]), "CDN IP must appear in errors"
    mock_db.upsert_host.assert_called_once()  # only called for legit IP
