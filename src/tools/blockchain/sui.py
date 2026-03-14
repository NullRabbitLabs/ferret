"""
Sui blockchain tool implementations.

Queries the Sui JSON-RPC API for validator set information.
Results are cached for 1 hour (epoch duration).
"""

import ipaddress
import logging
import re
import time
from collections.abc import Callable

import httpx
import yaml

from src.tools.blockchain.base import ChainTools

logger = logging.getLogger(__name__)

SUI_GET_VALIDATORS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "sui_get_validators",
        "description": (
            "Fetch the current active validator set from the Sui blockchain. "
            "Returns pubkey, net_address, p2p_address, worker_addresses, voting_power, "
            "and other validator metadata. Results cached for 1 hour."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}

SUI_SEED_PEER_URLS = {
    "sui": "https://raw.githubusercontent.com/MystenLabs/sui/main/nre/config/mainnet/fullnode.yaml",
    "sui-testnet": "https://raw.githubusercontent.com/MystenLabs/sui/main/nre/config/testnet/fullnode.yaml",
    "sui-devnet": "https://raw.githubusercontent.com/MystenLabs/sui/main/nre/config/devnet/fullnode.yaml",
}

SUI_ENUMERATE_PEERS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "sui_enumerate_peers",
        "description": (
            "Scrape Prometheus metrics from a known Sui node to discover connected peer IPs. "
            "Parses network_peer_connected metrics to extract peer addresses. "
            "Requires a metrics endpoint URL (typically port 9184)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "metrics_url": {
                    "type": "string",
                    "description": "Prometheus metrics endpoint URL (e.g. http://host:9184/metrics)",
                },
            },
            "required": ["metrics_url"],
        },
    },
}

SUI_GET_COMMITTEE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "sui_get_committee",
        "description": (
            "Fetch the Sui committee info for a given epoch (or current epoch if not specified). "
            "Returns {pubkey, stake} for each committee member."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "epoch": {
                    "type": "integer",
                    "description": "Epoch number (null for current epoch)",
                },
            },
            "required": [],
        },
    },
}


class SuiTools(ChainTools):
    """Sui blockchain discovery tools."""

    def __init__(self, rpc_url: str, cache_ttl: int = 3600) -> None:
        self._rpc_url = rpc_url
        self._cache_ttl = cache_ttl
        self._validators_cache: dict | None = None
        self._validators_cached_at: float = 0.0

    def schemas(self) -> list[dict]:
        return [SUI_GET_VALIDATORS_SCHEMA, SUI_GET_COMMITTEE_SCHEMA, SUI_ENUMERATE_PEERS_SCHEMA]

    def primary_tool_name(self) -> str:
        return "sui_get_validators"

    async def get_seed_hosts(self, network: str) -> list[dict]:
        """Fetch validators + seed peers and return merged, deduplicated host list.

        IP addresses are stored as ip_address; dns4 hostnames are stored as
        hostname (no DNS resolution — the DB now supports hostname-only records).
        Deduplicates net/p2p entries that point to the same host:port.
        """
        result = await self.get_validators()
        seen: set[tuple] = set()
        hosts = []

        for v in result.get("validators", []):
            pubkey = v.get("pubkey")
            name = v.get("name")
            label = name or (pubkey[:10] if pubkey else "unknown")

            for raw_host, port, stype in (
                (v.get("net_host"), v.get("net_port"), "rpc"),
                (v.get("p2p_host"), v.get("p2p_port"), "p2p"),
            ):
                if not raw_host:
                    continue
                key = (raw_host, port)
                if key in seen:
                    continue
                seen.add(key)

                is_ip = _is_ip(raw_host)
                hosts.append({
                    "ip_address": raw_host if is_ip else None,
                    "hostname": None if is_ip else raw_host,
                    "port": port,
                    "service_type": stype,
                    "confidence": 0.95,
                    "discovery_method": "on_chain",
                    "validator_pubkey": pubkey,
                    "operator_name": name,
                    "reasoning": f"On-chain {stype} address for {label}",
                })

        # Merge seed peers (fullnodes from GitHub config)
        seed_peers = await self.get_seed_peers(network)
        for peer in seed_peers:
            key = (peer.get("ip_address") or peer.get("hostname"), peer.get("port"))
            if key in seen:
                continue
            seen.add(key)
            hosts.append(peer)

        return hosts

    async def get_seed_peers(self, network: str) -> list[dict]:
        """Fetch seed peer list from MystenLabs GitHub fullnode config.

        Parses the p2p-config.seed-peers YAML entries and extracts multiaddrs.
        Returns host dicts with discovery_method='seed_peer', confidence=0.80.
        """
        url = SUI_SEED_PEER_URLS.get(network)
        if not url:
            # Fall back to mainnet config for unknown network variants
            url = SUI_SEED_PEER_URLS["sui"]

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(url)
            response.raise_for_status()
        except Exception:
            logger.warning("Failed to fetch seed peers from %s", url)
            return []

        try:
            data = yaml.safe_load(response.text)
        except yaml.YAMLError:
            logger.warning("Failed to parse seed peer YAML from %s", url)
            return []

        seed_peers = (data or {}).get("p2p-config", {}).get("seed-peers", [])
        hosts = []
        for entry in seed_peers:
            addr = entry.get("address")
            host, port = _parse_multiaddr(addr)
            if not host:
                continue
            is_ip = _is_ip(host)
            hosts.append({
                "ip_address": host if is_ip else None,
                "hostname": None if is_ip else host,
                "port": port,
                "service_type": "p2p",
                "confidence": 0.80,
                "discovery_method": "seed_peer",
                "validator_pubkey": None,
                "operator_name": None,
                "reasoning": f"Seed peer from MystenLabs GitHub config ({network})",
            })
        return hosts

    async def enumerate_peers(self, metrics_url: str, **kwargs) -> dict:
        """Scrape Prometheus metrics from a Sui node to discover connected peers.

        Parses network_peer_connected{peer_id="...",address="..."} lines.
        Only returns peers with value=1 (currently connected).
        """
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(metrics_url)
            response.raise_for_status()
        except Exception:
            logger.warning("Failed to scrape metrics from %s", metrics_url)
            return {"peers": [], "metrics_url": metrics_url}

        peers = []
        seen: set[tuple] = set()
        for line in response.text.splitlines():
            if not line.startswith("network_peer_connected{"):
                continue
            # Only include connected peers (value == 1)
            parts = line.rsplit(" ", 1)
            if len(parts) != 2:
                continue
            try:
                value = float(parts[1])
            except ValueError:
                continue
            if value != 1:
                continue

            # Extract address from labels
            m = re.search(r'address="([^"]+)"', line)
            if not m:
                continue
            host, port = _parse_multiaddr(m.group(1))
            if not host:
                continue
            key = (host, port)
            if key in seen:
                continue
            seen.add(key)

            is_ip = _is_ip(host)
            peers.append({
                "ip_address": host if is_ip else None,
                "hostname": None if is_ip else host,
                "port": port,
                "service_type": "p2p",
                "confidence": 0.70,
                "discovery_method": "peer_enumeration",
            })

        return {"peers": peers, "metrics_url": metrics_url, "count": len(peers)}

    def get_tool_map(self) -> dict[str, Callable]:
        return {
            "sui_get_validators": self.get_validators,
            "sui_get_committee": self.get_committee,
            "sui_enumerate_peers": self.enumerate_peers,
        }

    def _is_validators_cache_valid(self) -> bool:
        return (
            self._validators_cache is not None
            and (time.monotonic() - self._validators_cached_at) < self._cache_ttl
        )

    async def get_validators(self, **kwargs) -> dict:
        """Fetch active validators from suix_getLatestSuiSystemState."""
        if self._is_validators_cache_valid():
            return self._validators_cache  # type: ignore[return-value]

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                self._rpc_url,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "suix_getLatestSuiSystemState",
                    "params": [],
                },
            )
        response.raise_for_status()
        data = response.json()

        active_validators = data.get("result", {}).get("activeValidators", [])
        parsed = []
        for v in active_validators:
            net_host, net_port = _parse_multiaddr(v.get("netAddress"))
            p2p_host, p2p_port = _parse_multiaddr(v.get("p2pAddress"))
            parsed.append(
                {
                    "pubkey": v.get("suiAddress"),
                    "name": v.get("name"),
                    "net_host": net_host,
                    "net_port": net_port,
                    "p2p_host": p2p_host,
                    "p2p_port": p2p_port,
                }
            )

        result = {"validators": parsed, "count": len(parsed)}
        self._validators_cache = result
        self._validators_cached_at = time.monotonic()
        return result

    async def get_committee(self, epoch: int | None = None, **kwargs) -> dict:
        """Fetch committee info from suix_getCommitteeInfo."""
        params = [epoch] if epoch is not None else []
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                self._rpc_url,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "suix_getCommitteeInfo",
                    "params": params,
                },
            )
        response.raise_for_status()
        data = response.json()

        result_data = data.get("result", {})
        validators = result_data.get("validators", [])
        committee = [
            {"pubkey": entry[0], "stake": entry[1]}
            for entry in validators
            if isinstance(entry, (list, tuple)) and len(entry) >= 2
        ]
        return {"epoch": result_data.get("epoch"), "committee": committee}


def _parse_multiaddr(addr: str | None) -> tuple[str | None, int | None]:
    """Parse a Sui multiaddr string into (host, port).

    Handles formats like:
      /ip4/1.2.3.4/tcp/8080/http
      /dns4/validator.example.com/tcp/8080
      /ip6/::1/tcp/8080
      /ip4/1.2.3.4/udp/8084/quic-v1   (newer Sui QUIC transport)
    """
    if not addr:
        return None, None
    m = re.search(r"/(?:ip4|ip6|dns4?)/([^/]+)/tcp/(\d+)", addr)
    if m:
        return m.group(1), int(m.group(2))
    m = re.search(r"/(?:ip4|ip6|dns4?)/([^/]+)/udp/(\d+)", addr)
    if m:
        return m.group(1), int(m.group(2))
    return None, None


def _is_ip(host: str) -> bool:
    """Return True if host is an IP address (v4 or v6), False if it's a hostname."""
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False
