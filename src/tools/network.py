"""
Network intelligence tools: ASN lookup, cert transparency, WHOIS, subnet probe.
"""

import asyncio
import ipaddress
import time
from typing import Any

import dns.exception
import dns.resolver
import httpx
import whois

from src.tools.base import BaseTool
from src.tools.schemas import (
    ASN_LOOKUP_SCHEMA,
    CERT_TRANSPARENCY_SEARCH_SCHEMA,
    SUBNET_PROBE_SCHEMA,
    WHOIS_LOOKUP_SCHEMA,
)

# Default allowed ports per chain type — tools check against this at runtime
DEFAULT_ALLOWED_PORTS: set[int] = {
    # Sui
    8080, 8081, 8082, 8083, 8084, 9184, 1337,
    # Solana
    8000, 8001, 8002, 8003, 8899, 8900, 9900, 8328,
}

# Residential IP ranges to exclude from subnet_probe
_RESIDENTIAL_RANGES = [
    ipaddress.ip_network("100.64.0.0/10"),   # Shared address space (CGNAT)
    ipaddress.ip_network("192.168.0.0/16"),  # Private
    ipaddress.ip_network("10.0.0.0/8"),      # Private
    ipaddress.ip_network("172.16.0.0/12"),   # Private
    ipaddress.ip_network("127.0.0.0/8"),     # Loopback
]


class AsnLookupTool(BaseTool):
    """
    ASN lookup via Team Cymru DNS.

    IP → query {reversed}.origin.asn.cymru.com TXT
    ASN → query AS{n}.asn.cymru.com TXT for org info
    """

    rate_limit = 5.0

    @property
    def schema(self) -> dict:
        return ASN_LOOKUP_SCHEMA

    async def execute(self, query: str, **kwargs) -> dict:
        await self._rate_limit()
        query = query.strip()

        if query.upper().startswith("AS"):
            return await self._lookup_asn(query.upper().lstrip("AS"))

        try:
            ipaddress.ip_address(query)
            return await self._lookup_ip(query)
        except ValueError:
            return {"error": f"Invalid query: {query!r} — must be an IP address or ASN like AS13335"}

    async def _lookup_ip(self, ip: str) -> dict:
        """IP → ASN via origin.asn.cymru.com TXT."""
        try:
            addr = ipaddress.ip_address(ip)
            if isinstance(addr, ipaddress.IPv4Address):
                reversed_parts = ".".join(reversed(ip.split(".")))
                cymru_host = f"{reversed_parts}.origin.asn.cymru.com"
            else:
                # IPv6: expand, reverse nibbles
                expanded = addr.exploded.replace(":", "")
                reversed_nibbles = ".".join(reversed(expanded))
                cymru_host = f"{reversed_nibbles}.origin6.asn.cymru.com"

            resolver = dns.resolver.Resolver()
            resolver.lifetime = 5.0
            answers = resolver.resolve(cymru_host, "TXT")
            txt = str(answers[0]).strip('"')
            # Format: "ASN | prefix | CC | registry | allocated"
            parts = [p.strip() for p in txt.split("|")]
            if len(parts) >= 2:
                asn = parts[0].strip()
                prefix = parts[1].strip() if len(parts) > 1 else None
                country = parts[2].strip() if len(parts) > 2 else None

                org_result = await self._lookup_asn(asn.lstrip("AS"))
                return {
                    "ip": ip,
                    "asn": asn,
                    "prefix": prefix,
                    "country": country,
                    "as_org": org_result.get("as_org"),
                }
            return {"ip": ip, "raw": txt}
        except dns.exception.Timeout:
            return {"ip": ip, "error": "Timeout querying Cymru DNS"}
        except dns.resolver.NXDOMAIN:
            return {"ip": ip, "error": "No ASN record found"}
        except Exception as e:
            return {"ip": ip, "error": str(e)}

    async def _lookup_asn(self, asn_number: str) -> dict:
        """ASN → org name via asn.cymru.com TXT."""
        try:
            cymru_host = f"AS{asn_number}.asn.cymru.com"
            resolver = dns.resolver.Resolver()
            resolver.lifetime = 5.0
            answers = resolver.resolve(cymru_host, "TXT")
            txt = str(answers[0]).strip('"')
            # Format: "ASN | CC | registry | allocated | AS Name"
            parts = [p.strip() for p in txt.split("|")]
            as_org = parts[4].strip() if len(parts) > 4 else txt
            return {"asn": f"AS{asn_number}", "as_org": as_org}
        except Exception as e:
            return {"asn": f"AS{asn_number}", "error": str(e)}


_HOSTING_PROVIDER_DOMAINS = {
    # Cloud compute — wildcard searches return millions of unrelated certs
    "amazonaws.com", "compute.amazonaws.com",
    "digitalocean.com",
    "linode.com", "akamai.com",
    "vultr.com",
    "hetzner.com", "hetzner.net",
    "ovh.com", "ovh.net",
    "cherryservers.com", "cherryservers.net",
    "contabo.com",
    "leaseweb.com", "leaseweb.net",
    "scaleway.com",
    "cloudapp.net", "azurewebsites.net",
    "googleusercontent.com",
    "softlayer.com",
    "multacom.com",
}


def _is_hosting_provider_query(query: str) -> bool:
    """Return True if query targets a generic hosting provider domain."""
    domain = query.lstrip("%*.").lower()
    return any(domain == h or domain.endswith("." + h) for h in _HOSTING_PROVIDER_DOMAINS)


class CertTransparencySearchTool(BaseTool):
    """
    Certificate transparency search via crt.sh.

    Results are cached for 24 hours.
    Rate limit: 2/s.
    """

    rate_limit = 2.0
    _CACHE_TTL = 86400  # 24 hours

    def __init__(self) -> None:
        super().__init__()
        self._cache: dict[str, tuple[float, list]] = {}

    @property
    def schema(self) -> dict:
        return CERT_TRANSPARENCY_SEARCH_SCHEMA

    def _get_cached(self, query: str) -> list | None:
        if query in self._cache:
            cached_at, data = self._cache[query]
            if time.monotonic() - cached_at < self._CACHE_TTL:
                return data
        return None

    async def execute(self, query: str, **kwargs) -> dict:
        if _is_hosting_provider_query(query):
            return {
                "query": query,
                "results": [],
                "error": (
                    f"Rejected: '{query}' targets a generic hosting provider. "
                    "CT searches must use specific operator domains, not cloud/VPS provider wildcards."
                ),
            }
        # Reject bare keywords with no dot — not a domain name
        stripped = query.lstrip("%*.")
        if "." not in stripped:
            return {
                "query": query,
                "results": [],
                "error": (
                    f"Rejected: '{query}' is not a domain name. "
                    "CT searches must use a specific operator domain (e.g. '%.myoperator.io')."
                ),
            }

        cached = self._get_cached(query)
        if cached is not None:
            return {"query": query, "results": cached, "cached": True}

        await self._rate_limit()
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(
                    "https://crt.sh/",
                    params={"q": query, "output": "json"},
                )
            response.raise_for_status()
            raw = response.json()
            seen_keys: set[tuple] = set()
            results = []
            for entry in (raw if isinstance(raw, list) else []):
                key = (entry.get("common_name"), entry.get("not_before"))
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                results.append({
                    "common_name": entry.get("common_name"),
                    "sans": entry.get("name_value", "").split("\n"),
                    "issuer": entry.get("issuer_name"),
                    "not_before": entry.get("not_before"),
                    "not_after": entry.get("not_after"),
                })
            self._cache[query] = (time.monotonic(), results)
            return {"query": query, "results": results, "cached": False}
        except httpx.TimeoutException:
            return {"query": query, "results": [], "note": "crt.sh timed out — skipping CT search"}
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 503:
                return {"query": query, "results": [], "note": "crt.sh unavailable (503). Skip CT searches and use other tools."}
            if e.response.status_code >= 500:
                return {"query": query, "results": [], "note": f"crt.sh returned {e.response.status_code} — skipping CT search"}
            return {"query": query, "results": [], "error": str(e)}
        except Exception as e:
            return {"query": query, "results": [], "error": str(e)}


def _is_ip_address(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def _is_hosting_provider_domain(value: str) -> bool:
    """Return True if value is a hostname under a known hosting provider."""
    domain = value.lstrip("%*.").lower()
    return any(domain == h or domain.endswith("." + h) for h in _HOSTING_PROVIDER_DOMAINS)


class WhoisLookupTool(BaseTool):
    """WHOIS lookup. Rate limit: 1/s."""

    rate_limit = 1.0

    @property
    def schema(self) -> dict:
        return WHOIS_LOOKUP_SCHEMA

    async def execute(self, query: str, **kwargs) -> dict:
        if _is_ip_address(query):
            return {
                "query": query,
                "error": (
                    f"Rejected: '{query}' is an IP address. WHOIS on IPs gives ISP info only — "
                    "use asn_lookup for that. Query operator domain names instead."
                ),
            }
        if _is_hosting_provider_domain(query):
            return {
                "query": query,
                "error": (
                    f"Rejected: '{query}' is a hosting provider hostname with no useful registrant info. "
                    "WHOIS must target operator-owned domains."
                ),
            }
        await self._rate_limit()
        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(None, whois.whois, query)
            return {
                "query": query,
                "registrant": getattr(result, "org", None) or getattr(result, "registrant", None),
                "creation_date": str(getattr(result, "creation_date", None)),
                "expiration_date": str(getattr(result, "expiration_date", None)),
                "nameservers": getattr(result, "name_servers", []),
                "registrar": getattr(result, "registrar", None),
            }
        except Exception as e:
            return {"query": query, "error": str(e)}


class SubnetProbeTool(BaseTool):
    """
    Active TCP connect scan of a subnet.

    Enforces:
    - Maximum /24 CIDR (256 hosts)
    - Ports must be from the configured allowlist
    - No residential/private IP ranges
    - 50 connections/second rate limit
    """

    rate_limit = 50.0
    BANNER_TIMEOUT = 2.0
    CONNECT_TIMEOUT = 3.0

    def __init__(self, allowed_ports: set[int] | None = None) -> None:
        super().__init__()
        self._allowed_ports = allowed_ports if allowed_ports is not None else DEFAULT_ALLOWED_PORTS

    @property
    def schema(self) -> dict:
        return SUBNET_PROBE_SCHEMA

    def _validate_cidr(self, cidr: str) -> ipaddress.IPv4Network:
        try:
            network = ipaddress.IPv4Network(cidr, strict=False)
        except ValueError:
            raise ValueError(f"Invalid CIDR: {cidr!r}")

        if network.prefixlen < 24:
            raise ValueError(
                f"CIDR {cidr} is larger than /24. subnet_probe max is /24 (256 hosts)."
            )
        return network

    def _validate_ports(self, ports: list[int]) -> None:
        invalid = [p for p in ports if p not in self._allowed_ports]
        if invalid:
            raise ValueError(
                f"Ports {invalid} are not in the allowlist. "
                f"Allowed ports: {sorted(self._allowed_ports)}"
            )

    def _is_residential(self, network: ipaddress.IPv4Network) -> bool:
        for res_range in _RESIDENTIAL_RANGES:
            if network.overlaps(res_range):
                return True
        return False

    async def _probe_one(self, ip: str, port: int) -> dict:
        await self._rate_limit()
        result: dict[str, Any] = {"ip": ip, "port": port, "open": False, "banner": None}
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(ip, port),
                timeout=self.CONNECT_TIMEOUT,
            )
            result["open"] = True
            try:
                banner_bytes = await asyncio.wait_for(
                    reader.read(256), timeout=self.BANNER_TIMEOUT
                )
                result["banner"] = banner_bytes.decode("utf-8", errors="replace").strip()
            except asyncio.TimeoutError:
                pass
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
        except (asyncio.TimeoutError, ConnectionRefusedError, OSError):
            pass
        return result

    async def execute(self, cidr: str, ports: list[int], **kwargs) -> dict:
        network = self._validate_cidr(cidr)
        self._validate_ports(ports)

        if self._is_residential(network):
            raise ValueError(f"CIDR {cidr} overlaps with residential/private ranges")

        hosts = list(network.hosts())
        tasks = [
            self._probe_one(str(host), port)
            for host in hosts
            for port in ports
        ]
        results = await asyncio.gather(*tasks, return_exceptions=False)
        open_results = [r for r in results if r["open"]]
        return {
            "cidr": cidr,
            "ports": ports,
            "total_probed": len(tasks),
            "open_count": len(open_results),
            "results": open_results,
        }
