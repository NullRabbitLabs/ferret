"""
Cosmos Hub blockchain tool implementations.

Fetches seed nodes and persistent peers from the cosmos/chain-registry GitHub repo.
Results are cached for 1 hour (peer sets change slowly).
"""

import logging
import re
import time
from collections.abc import Callable

import httpx

from src.tools.blockchain.base import ChainTools

logger = logging.getLogger(__name__)

_CHAIN_REGISTRY_URL = (
    "https://raw.githubusercontent.com/cosmos/chain-registry/master/cosmoshub/chain.json"
)

_IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")

COSMOS_GET_CHAIN_REGISTRY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "cosmos_get_chain_registry",
        "description": (
            "Fetch seed nodes and persistent peers from the cosmos/chain-registry GitHub repo. "
            "Returns hostnames/IPs and P2P ports for all advertised Cosmos Hub peers. "
            "Results cached for 1 hour."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}


class CosmosTools(ChainTools):
    """Cosmos Hub blockchain discovery tools."""

    def __init__(self, cache_ttl: int = 3600) -> None:
        self._cache_ttl = cache_ttl
        self._registry_cache: dict | None = None
        self._registry_cached_at: float = 0.0

    def schemas(self) -> list[dict]:
        return [COSMOS_GET_CHAIN_REGISTRY_SCHEMA]

    def primary_tool_name(self) -> str:
        return "cosmos_get_chain_registry"

    def get_tool_map(self) -> dict[str, Callable]:
        return {"cosmos_get_chain_registry": self._fetch_chain_registry}

    async def get_seed_hosts(self, network: str) -> list[dict]:
        """Fetch peers from chain registry and return host list for bulk import.

        Deduplicates by (hostname or ip_address, port).
        Entries with no parseable address are skipped.
        """
        data = await self._fetch_chain_registry()
        hosts = []
        seen: set[tuple] = set()

        peers = data.get("peers", {})
        all_peers = peers.get("seeds", []) + peers.get("persistent_peers", [])

        for entry in all_peers:
            address = entry.get("address", "")
            hostname, ip_address, port = _parse_peer_address(address)
            if not hostname and not ip_address:
                continue
            key = (hostname or ip_address, port)
            if key in seen:
                continue
            seen.add(key)

            node_id = entry.get("id", "")
            provider = entry.get("provider", "")
            hosts.append({
                "ip_address": ip_address,
                "hostname": hostname,
                "port": port or 26656,
                "service_type": "p2p",
                "confidence": 0.85,
                "discovery_method": "on_chain",
                "validator_pubkey": node_id,
                "reasoning": f"Cosmos chain registry peer {provider!r}",
            })

        return hosts

    def _is_cache_valid(self) -> bool:
        return (
            self._registry_cache is not None
            and (time.monotonic() - self._registry_cached_at) < self._cache_ttl
        )

    async def _fetch_chain_registry(self, **kwargs) -> dict:
        """Fetch chain.json from cosmos/chain-registry. Returns {} on any error."""
        if self._is_cache_valid():
            return self._registry_cache  # type: ignore[return-value]

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(_CHAIN_REGISTRY_URL)
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            logger.warning("cosmos chain registry fetch failed error=%s", e)
            return {}

        self._registry_cache = data
        self._registry_cached_at = time.monotonic()
        return data


def _parse_peer_address(address: str) -> tuple[str | None, str | None, int | None]:
    """Parse a chain registry peer address into (hostname, ip_address, port).

    Accepts 'host:port' format only. Returns (None, None, None) if unparseable.
    - If host matches IPv4 pattern → ip_address is set, hostname is None
    - Otherwise → hostname is set, ip_address is None
    """
    if not address or ":" not in address:
        return None, None, None

    host, _, port_str = address.rpartition(":")
    if not host or not port_str.isdigit():
        return None, None, None

    port = int(port_str)

    if _IPV4_RE.match(host):
        return None, host, port
    return host, None, port
