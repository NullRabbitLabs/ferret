"""
Tests for DNS tools.

Mocks dns.resolver.resolve to avoid real network calls.
"""

import asyncio
from unittest.mock import MagicMock, patch

import dns.resolver
import pytest


@pytest.fixture
def dns_lookup_tool():
    from src.tools.dns import DnsLookupTool
    return DnsLookupTool()


@pytest.fixture
def reverse_dns_tool():
    from src.tools.dns import ReverseDnsTool
    return ReverseDnsTool()


def _mock_dns_answer(records: list[str], ttl: int = 300):
    """Build a mock dns.resolver.Answer-like object."""
    mock_answer = MagicMock()
    mock_answer.ttl = ttl
    mock_rdata_list = []
    for r in records:
        rdata = MagicMock()
        rdata.__str__ = MagicMock(return_value=r)
        mock_rdata_list.append(rdata)
    mock_answer.__iter__ = MagicMock(return_value=iter(mock_rdata_list))
    return mock_answer


@pytest.mark.asyncio
async def test_dns_lookup_returns_records(dns_lookup_tool):
    answer = _mock_dns_answer(["1.2.3.4", "5.6.7.8"])
    with patch("dns.resolver.Resolver") as mock_resolver_cls:
        resolver = MagicMock()
        resolver.resolve.return_value = answer
        mock_resolver_cls.return_value = resolver

        result = await dns_lookup_tool.execute(hostname="example.com", record_type="A")

    assert result["hostname"] == "example.com"
    assert result["record_type"] == "A"
    assert len(result["records"]) == 2
    assert result["records"][0]["value"] == "1.2.3.4"
    assert result["records"][0]["ttl"] == 300


@pytest.mark.asyncio
async def test_dns_lookup_handles_nxdomain(dns_lookup_tool):
    with patch("dns.resolver.Resolver") as mock_resolver_cls:
        resolver = MagicMock()
        resolver.resolve.side_effect = dns.resolver.NXDOMAIN()
        mock_resolver_cls.return_value = resolver

        result = await dns_lookup_tool.execute(hostname="notexist.example.com", record_type="A")

    assert result["records"] == []
    assert result["error"] == "NXDOMAIN"


@pytest.mark.asyncio
async def test_dns_lookup_sets_timeout(dns_lookup_tool):
    answer = _mock_dns_answer(["1.2.3.4"])
    with patch("dns.resolver.Resolver") as mock_resolver_cls:
        resolver = MagicMock()
        resolver.resolve.return_value = answer
        mock_resolver_cls.return_value = resolver

        await dns_lookup_tool.execute(hostname="example.com", record_type="A")

    assert resolver.lifetime == 5.0


@pytest.mark.asyncio
async def test_reverse_dns_returns_hostnames(reverse_dns_tool):
    answer = _mock_dns_answer(["host.example.com."])
    with patch("dns.resolver.Resolver") as mock_resolver_cls, \
         patch("dns.reversename.from_address") as mock_rev:
        resolver = MagicMock()
        resolver.resolve.return_value = answer
        mock_resolver_cls.return_value = resolver
        mock_rev.return_value = "4.3.2.1.in-addr.arpa."

        result = await reverse_dns_tool.execute(ip_address="1.2.3.4")

    assert result["ip_address"] == "1.2.3.4"
    assert "host.example.com." in result["hostnames"]


@pytest.mark.asyncio
async def test_reverse_dns_handles_nxdomain(reverse_dns_tool):
    with patch("dns.resolver.Resolver") as mock_resolver_cls, \
         patch("dns.reversename.from_address") as mock_rev:
        resolver = MagicMock()
        resolver.resolve.side_effect = dns.resolver.NXDOMAIN()
        mock_resolver_cls.return_value = resolver
        mock_rev.return_value = "4.3.2.1.in-addr.arpa."

        result = await reverse_dns_tool.execute(ip_address="1.2.3.4")

    assert result["hostnames"] == []
    assert result["error"] == "NXDOMAIN"


@pytest.mark.asyncio
async def test_dns_lookup_timeout_returns_note_not_error(dns_lookup_tool):
    import dns.exception

    with patch("dns.resolver.Resolver") as mock_resolver_cls:
        resolver = MagicMock()
        resolver.resolve.side_effect = dns.exception.Timeout()
        mock_resolver_cls.return_value = resolver

        result = await dns_lookup_tool.execute(hostname="slow.example.com", record_type="A")

    assert "error" not in result, "DNS timeout must not be an error"
    assert "note" in result
    assert result["records"] == []


@pytest.mark.asyncio
async def test_reverse_dns_timeout_returns_note_not_error(reverse_dns_tool):
    import dns.exception

    with patch("dns.resolver.Resolver") as mock_resolver_cls, \
         patch("dns.reversename.from_address") as mock_rev:
        resolver = MagicMock()
        resolver.resolve.side_effect = dns.exception.Timeout()
        mock_resolver_cls.return_value = resolver
        mock_rev.return_value = "4.3.2.1.in-addr.arpa."

        result = await reverse_dns_tool.execute(ip_address="1.2.3.4")

    assert "error" not in result, "DNS timeout must not be an error"
    assert "note" in result
    assert result["hostnames"] == []


@pytest.mark.asyncio
async def test_dns_rate_limiter_fires():
    """Verify rate limiter calls asyncio.sleep when tokens are exhausted."""
    from src.tools.dns import DnsLookupTool

    tool = DnsLookupTool()
    tool.rate_limit = 1.0  # 1/s → each call after first needs a wait

    answer = _mock_dns_answer(["1.2.3.4"])
    sleep_calls = []

    async def mock_sleep(t):
        sleep_calls.append(t)

    with patch("dns.resolver.Resolver") as mock_resolver_cls, \
         patch("asyncio.sleep", side_effect=mock_sleep):
        resolver = MagicMock()
        resolver.resolve.return_value = answer
        mock_resolver_cls.return_value = resolver

        # Drain the initial token
        tool._tokens = 0.0
        tool._last_refill = __import__("time").monotonic()

        await tool.execute(hostname="example.com", record_type="A")

    assert len(sleep_calls) >= 1
    assert sleep_calls[0] > 0
