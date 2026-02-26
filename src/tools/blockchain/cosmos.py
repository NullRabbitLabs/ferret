"""
Cosmos Hub blockchain tool implementations.

Queries the CometBFT RPC /net_info endpoint for connected peers.
Results are cached for 1 hour (peer sets change slowly).
"""

import logging
import re
import time
from collections.abc import Callable

import httpx

from src.tools.blockchain.base import ChainTools

logger = logging.getLogger(__name__)

COSMOS_GET_NET_INFO_SCHEMA = {
    "type": "function",
    "function": {
        "name": "cosmos_get_net_info",
        "description": (
            "Fetch connected peers from the Cosmos Hub CometBFT RPC /net_info endpoint. "
            "Returns peer node IDs, IP addresses, and P2P listen addresses. "
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

    def __init__(self, rpc_url: str, cache_ttl: int = 3600) -> None:
        self._rpc_url = rpc_url.rstrip("/")
        self._cache_ttl = cache_ttl
        self._peers_cache: dict | None = None
        self._peers_cached_at: float = 0.0

    def schemas(self) -> list[dict]:
        return [COSMOS_GET_NET_INFO_SCHEMA]

    def primary_tool_name(self) -> str:
        return "cosmos_get_net_info"

    def get_tool_map(self) -> dict[str, Callable]:
        return {"cosmos_get_net_info": self.get_net_info}

    async def get_seed_hosts(self, network: str) -> list[dict]:
        """Fetch CometBFT peers and return host list for bulk import.

        Deduplicates by (remote_ip, port). Peers missing a remote_ip are skipped.
        """
        result = await self.get_net_info()
        hosts = []
        seen: set[tuple] = set()

        for peer in result.get("peers", []):
            ip = peer.get("remote_ip")
            if not ip:
                continue
            port = peer.get("p2p_port") or 26656
            key = (ip, port)
            if key in seen:
                continue
            seen.add(key)

            node_id = peer.get("node_id", "")
            moniker = peer.get("moniker", "")
            label = moniker or (node_id[:10] if node_id else "unknown")
            hosts.append({
                "ip_address": ip,
                "port": port,
                "service_type": "p2p",
                "confidence": 0.9,
                "discovery_method": "on_chain",
                "validator_pubkey": node_id,
                "reasoning": f"CometBFT peer {label!r} from net_info",
            })

        return hosts

    def _is_cache_valid(self) -> bool:
        return (
            self._peers_cache is not None
            and (time.monotonic() - self._peers_cached_at) < self._cache_ttl
        )

    async def get_net_info(self, **kwargs) -> dict:
        """Fetch peers from CometBFT RPC /net_info. Returns [] on any error."""
        if self._is_cache_valid():
            return self._peers_cache  # type: ignore[return-value]

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(f"{self._rpc_url}/net_info")
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            logger.warning("cosmos net_info failed rpc_url=%s error=%s", self._rpc_url, e)
            return {"peers": [], "count": 0}

        peers_raw = data.get("result", {}).get("peers", [])
        peers = []
        for p in peers_raw:
            node_info = p.get("node_info", {})
            remote_ip = p.get("remote_ip", "")
            listen_addr = node_info.get("listen_addr", "")
            port = _parse_port(listen_addr) or 26656
            peers.append({
                "remote_ip": remote_ip,
                "p2p_port": port,
                "node_id": node_info.get("id", ""),
                "moniker": node_info.get("moniker", ""),
                "listen_addr": listen_addr,
            })

        result = {"peers": peers, "count": len(peers)}
        self._peers_cache = result
        self._peers_cached_at = time.monotonic()
        return result


def _parse_port(listen_addr: str) -> int | None:
    """Extract port from a CometBFT listen_addr like 'tcp://0.0.0.0:26656'."""
    m = re.search(r":(\d+)$", listen_addr)
    if m:
        return int(m.group(1))
    return None
