"""
Tests for network intelligence tools: ASN, cert transparency, WHOIS, subnet_probe.
"""

import ipaddress
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ============================================================
# subnet_probe — constraint enforcement
# ============================================================

@pytest.fixture
def subnet_probe_tool():
    from src.tools.network import SubnetProbeTool, DEFAULT_ALLOWED_PORTS
    return SubnetProbeTool(allowed_ports=DEFAULT_ALLOWED_PORTS)


def test_subnet_probe_rejects_cidr_larger_than_24(subnet_probe_tool):
    with pytest.raises(ValueError, match="/24"):
        subnet_probe_tool._validate_cidr("10.0.0.0/23")


def test_subnet_probe_rejects_slash_16(subnet_probe_tool):
    with pytest.raises(ValueError, match="/24"):
        subnet_probe_tool._validate_cidr("1.2.0.0/16")


def test_subnet_probe_accepts_slash_24(subnet_probe_tool):
    network = subnet_probe_tool._validate_cidr("1.2.3.0/24")
    assert network.prefixlen == 24


def test_subnet_probe_accepts_slash_28(subnet_probe_tool):
    network = subnet_probe_tool._validate_cidr("1.2.3.0/28")
    assert network.prefixlen == 28


def test_subnet_probe_rejects_port_not_in_allowlist(subnet_probe_tool):
    with pytest.raises(ValueError, match="allowlist"):
        subnet_probe_tool._validate_ports([9999])


def test_subnet_probe_rejects_mixed_ports(subnet_probe_tool):
    with pytest.raises(ValueError, match="allowlist"):
        subnet_probe_tool._validate_ports([8080, 9999])


def test_subnet_probe_accepts_allowed_ports(subnet_probe_tool):
    # Should not raise
    subnet_probe_tool._validate_ports([8080, 8899, 8900])


def test_subnet_probe_rejects_private_range(subnet_probe_tool):
    private_network = ipaddress.IPv4Network("192.168.1.0/24")
    assert subnet_probe_tool._is_residential(private_network) is True


def test_subnet_probe_rejects_rfc1918_10x(subnet_probe_tool):
    private_network = ipaddress.IPv4Network("10.0.0.0/24")
    assert subnet_probe_tool._is_residential(private_network) is True


def test_subnet_probe_allows_public_range(subnet_probe_tool):
    public_network = ipaddress.IPv4Network("1.2.3.0/24")
    assert subnet_probe_tool._is_residential(public_network) is False


@pytest.mark.asyncio
async def test_subnet_probe_raises_for_residential_cidr(subnet_probe_tool):
    with pytest.raises(ValueError, match="residential"):
        await subnet_probe_tool.execute(cidr="192.168.1.0/24", ports=[8080])


@pytest.mark.asyncio
async def test_subnet_probe_returns_results_structure():
    from src.tools.network import SubnetProbeTool

    # Use a very small /30 subnet to limit the test surface
    tool = SubnetProbeTool(allowed_ports={8080})

    async def mock_probe_one(ip: str, port: int) -> dict:
        return {"ip": ip, "port": port, "open": False, "banner": None}

    with patch.object(tool, "_probe_one", side_effect=mock_probe_one):
        result = await tool.execute(cidr="1.2.3.0/30", ports=[8080])

    assert "results" in result
    assert "total_probed" in result
    assert result["cidr"] == "1.2.3.0/30"


@pytest.mark.asyncio
async def test_subnet_probe_results_contains_only_open_hosts():
    from src.tools.network import SubnetProbeTool

    tool = SubnetProbeTool(allowed_ports={8080})

    # 4 usable hosts in /30; only 1.2.3.2 is open
    async def mock_probe_one(ip: str, port: int) -> dict:
        return {"ip": ip, "port": port, "open": ip == "1.2.3.2", "banner": None}

    with patch.object(tool, "_probe_one", side_effect=mock_probe_one):
        result = await tool.execute(cidr="1.2.3.0/30", ports=[8080])

    assert result["total_probed"] == 2  # /30 has 2 usable hosts, 1 port = 2 probes
    assert result["open_count"] == 1
    assert len(result["results"]) == 1, "results must contain only open hosts"
    assert result["results"][0]["ip"] == "1.2.3.2"


# ============================================================
# WhoisLookup — hosting provider / IP guard
# ============================================================

@pytest.fixture
def whois_tool():
    from src.tools.network import WhoisLookupTool
    return WhoisLookupTool()


@pytest.mark.asyncio
@pytest.mark.parametrize("query", [
    "139.84.156.198",         # raw IP
    "34.245.29.28",           # AWS IP
    "2001:db8::1",            # IPv6
    "ec2-34-245-29-28.eu-west-1.compute.amazonaws.com",
    "1.vultr.com",
    "host.digitalocean.com",
    "srv.hetzner.com",
    "node.cherryservers.net",
])
async def test_whois_rejects_ip_and_hosting_provider_queries(whois_tool, query):
    """WHOIS on IPs or hosting provider hostnames returns no operator info — reject early."""
    result = await whois_tool.execute(query=query)
    assert "error" in result
    assert result["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("query", [
    "myoperator.io",
    "sui-validator.example.com",
    "blockdaemon.com",
])
async def test_whois_allows_operator_domains(whois_tool, query):
    """Operator-owned domains must pass through to whois."""
    with patch("whois.whois") as mock_whois:
        mock_whois.return_value = MagicMock(
            org="Some Org", registrant=None, creation_date=None,
            expiration_date=None, name_servers=[], registrar="Registrar Inc",
        )
        result = await whois_tool.execute(query=query)
    assert "error" not in result


# ============================================================
# CertTransparencySearch — query structure guard
# ============================================================

@pytest.mark.asyncio
@pytest.mark.parametrize("query", [
    "sui",
    "solana",
    "validator",
    "rpc",
    "node",
])
async def test_cert_transparency_rejects_non_domain_queries(ct_tool, query):
    """CT search with bare keywords (no dot / no TLD) must be rejected."""
    result = await ct_tool.execute(query=query)
    assert result["results"] == []
    assert "error" in result


@pytest.mark.asyncio
@pytest.mark.parametrize("query", [
    "sui.io",
    "%.myoperator.io",
    "validator.example.com",
])
async def test_cert_transparency_accepts_valid_domain_queries(ct_tool, query):
    """Queries that look like real domains must pass through."""
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = []

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_cls.return_value = mock_client
        result = await ct_tool.execute(query=query)

    assert "error" not in result


# ============================================================
# ASN lookup — parsing
# ============================================================

@pytest.fixture
def asn_tool():
    from src.tools.network import AsnLookupTool
    return AsnLookupTool()


def _make_txt_rdata(txt: str) -> MagicMock:
    """Build a mock DNS TXT rdata whose str() returns txt."""
    rdata = MagicMock()
    rdata.__str__ = MagicMock(return_value=txt)
    rdata.configure_mock(**{"__str__.return_value": txt})
    return rdata


@pytest.mark.asyncio
async def test_asn_lookup_parses_cymru_txt_record(asn_tool):
    """Verify parsing of Cymru DNS TXT record format: ASN | prefix | CC | registry | allocated"""
    ip_rdata = _make_txt_rdata('"13335 | 1.1.1.0/24 | AU | apnic | 2011-08-11"')
    asn_rdata = _make_txt_rdata('"13335 | AU | apnic | 2010-07-14 | CLOUDFLARENET, US"')

    ip_answer = MagicMock()
    ip_answer.__iter__ = MagicMock(return_value=iter([ip_rdata]))
    ip_answer.__getitem__ = MagicMock(return_value=ip_rdata)

    asn_answer = MagicMock()
    asn_answer.__iter__ = MagicMock(return_value=iter([asn_rdata]))
    asn_answer.__getitem__ = MagicMock(return_value=asn_rdata)

    def mock_resolve(host, record_type):
        if "origin" in host:
            return ip_answer
        return asn_answer

    with patch("dns.resolver.Resolver") as mock_cls:
        resolver = MagicMock()
        resolver.resolve.side_effect = mock_resolve
        mock_cls.return_value = resolver

        result = await asn_tool.execute(query="1.1.1.1")

    assert result["ip"] == "1.1.1.1"
    assert "13335" in str(result.get("asn", ""))
    assert "CLOUDFLARENET" in str(result.get("as_org", ""))


@pytest.mark.asyncio
async def test_asn_lookup_rejects_invalid_query(asn_tool):
    result = await asn_tool.execute(query="not-an-ip-or-asn")
    assert "error" in result


# ============================================================
# CertTransparencySearch — caching
# ============================================================

@pytest.fixture
def ct_tool():
    from src.tools.network import CertTransparencySearchTool
    return CertTransparencySearchTool()


@pytest.mark.asyncio
async def test_cert_transparency_caches_results(ct_tool):
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = [
        {
            "common_name": "test.example.com",
            "name_value": "test.example.com",
            "issuer_name": "Let's Encrypt",
            "not_before": "2026-01-01",
            "not_after": "2026-04-01",
        }
    ]

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_cls.return_value = mock_client

        result1 = await ct_tool.execute(query="test.example.com")
        result2 = await ct_tool.execute(query="test.example.com")

    # Only one HTTP call
    assert mock_client.get.call_count == 1
    assert result1["results"] == result2["results"]
    assert result2["cached"] is True


@pytest.mark.asyncio
async def test_cert_transparency_timeout_returns_note_not_error(ct_tool):
    import httpx

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
        mock_cls.return_value = mock_client

        result = await ct_tool.execute(query="example.com")

    assert "error" not in result, "timeout must not be treated as an error"
    assert "note" in result
    assert result["results"] == []


@pytest.mark.asyncio
async def test_cert_transparency_http_503_returns_note_not_error(ct_tool):
    import httpx

    mock_response = MagicMock()
    mock_response.status_code = 503
    error = httpx.HTTPStatusError(
        "503 Service Unavailable",
        request=MagicMock(),
        response=mock_response,
    )
    mock_response.raise_for_status.side_effect = error

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_cls.return_value = mock_client

        result = await ct_tool.execute(query="example.com")

    assert "error" not in result, "HTTP 5xx must not be treated as a fatal error"
    assert "note" in result
    assert "503" in result["note"]
    assert result["results"] == []


# ============================================================
# CertTransparencySearch — hosting provider guard
# ============================================================

@pytest.mark.asyncio
@pytest.mark.parametrize("query", [
    "%.compute.amazonaws.com",
    "%.amazonaws.com",
    "%.digitalocean.com",
    "%.cherryservers.net",
    "%.hetzner.com",
    "%.vultr.com",
    "%.linode.com",
    "%.ovh.net",
    "%.contabo.com",
    "%.scaleway.com",
    "compute.amazonaws.com",
    "cherryservers.net",
])
async def test_cert_transparency_rejects_hosting_provider_domains(ct_tool, query):
    """CT search must reject queries for generic hosting providers — they return noise."""
    result = await ct_tool.execute(query=query)
    assert result["results"] == []
    assert "error" in result
    assert "hosting provider" in result["error"].lower() or "generic" in result["error"].lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("query", [
    "%.myvalidator.io",
    "validator.someoperator.com",
    "%.sui-validator.net",
    "rpc.blockdaemon.com",
])
async def test_cert_transparency_allows_operator_specific_domains(ct_tool, query):
    """Operator-specific domains must pass through to crt.sh."""
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = []

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_cls.return_value = mock_client

        result = await ct_tool.execute(query=query)

    assert "error" not in result


@pytest.mark.asyncio
async def test_cert_transparency_returns_parsed_fields(ct_tool):
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = [
        {
            "common_name": "*.validator.example.com",
            "name_value": "*.validator.example.com\nvalidator.example.com",
            "issuer_name": "DigiCert",
            "not_before": "2025-01-01",
            "not_after": "2026-01-01",
        }
    ]

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_cls.return_value = mock_client

        result = await ct_tool.execute(query="validator.example.com")

    assert len(result["results"]) == 1
    cert = result["results"][0]
    assert cert["common_name"] == "*.validator.example.com"
    assert cert["issuer"] == "DigiCert"
    assert isinstance(cert["sans"], list)
