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

SUI_SEED_PEER_URL = (
    "https://raw.githubusercontent.com/MystenLabs/sui/main/"
    "docs/content/guides/operator/sui-full-node.mdx"
)

# Fallback seed peers from MystenLabs docs (stable Mysten-operated SSFN infrastructure).
# Updated 2026-03-14 from https://docs.sui.io/guides/operator/sui-full-node
_FALLBACK_SEED_PEERS: dict[str, list[dict]] = {
    "sui": [
        {"address": "/dns/mel-00.mainnet.sui.io/udp/8084", "peer-id": "d32b55bdf1737ec415df8c88b3bf91e194b59ee3127e3f38ea46fd88ba2e7849"},
        {"address": "/dns/ewr-00.mainnet.sui.io/udp/8084", "peer-id": "c7bf6cb93ca8fdda655c47ebb85ace28e6931464564332bf63e27e90199c50ee"},
        {"address": "/dns/ewr-01.mainnet.sui.io/udp/8084", "peer-id": "3227f8a05f0faa1a197c075d31135a366a1c6f3d4872cb8af66c14dea3e0eb66"},
        {"address": "/dns/lhr-00.mainnet.sui.io/udp/8084", "peer-id": "c619a5e0f8f36eac45118c1f8bda28f0f508e2839042781f1d4a9818043f732c"},
        {"address": "/dns/sui-mainnet-ssfn-1.nodeinfra.com/udp/8084", "peer-id": "0c52ca8d2b9f51be4a50eb44ace863c05aadc940a7bd15d4d3f498deb81d7fc6"},
        {"address": "/dns/sui-mainnet-ssfn-2.nodeinfra.com/udp/8084", "peer-id": "1dbc28c105aa7eb9d1d3ac07ae663ea638d91f2b99c076a52bbded296bd3ed5c"},
        {"address": "/dns/sui-mainnet-ssfn-ashburn-na.overclock.run/udp/8084", "peer-id": "5ff8461ab527a8f241767b268c7aaf24d0312c7b923913dd3c11ee67ef181e45"},
        {"address": "/dns/sui-mainnet-ssfn-dallas-na.overclock.run/udp/8084", "peer-id": "e1a4f40d66f1c89559a195352ba9ff84aec28abab1d3aa1c491901a252acefa6"},
        {"address": "/dns/ssn01.mainnet.sui.rpcpool.com/udp/8084", "peer-id": "fadb7ccb0b7fc99223419176e707f5122fef4ea686eb8e80d1778588bf5a0bcd"},
        {"address": "/dns/ssn02.mainnet.sui.rpcpool.com/udp/8084", "peer-id": "13783584a90025b87d4604f1991252221e5fd88cab40001642f4b00111ae9b7e"},
    ],
    "sui-testnet": [
        {"address": "/dns/yto-tnt-ssfn-01.testnet.sui.io/udp/8084", "peer-id": "2ed53564d5581ded9b6773970ac2f1c84d39f9edf01308ff5a1ffe09b1add7b3"},
        {"address": "/dns/yto-tnt-ssfn-00.testnet.sui.io/udp/8084", "peer-id": "6563732e5ab33b4ae09c73a98fd37499b71b8f03c27b5cc51acc26934974aff2"},
        {"address": "/dns/nrt-tnt-ssfn-00.testnet.sui.io/udp/8084", "peer-id": "23a1f7cd901b6277cbedaa986b3fc183f171d800cabba863d48f698f518967e1"},
        {"address": "/dns/ewr-tnt-ssfn-00.testnet.sui.io/udp/8084", "peer-id": "df8a8d128051c249e224f95fcc463f518a0ebed8986bbdcc11ed751181fecd38"},
        {"address": "/dns/lax-tnt-ssfn-00.testnet.sui.io/udp/8084", "peer-id": "f9a72a0a6c17eed09c27898eab389add704777c03e135846da2428f516a0c11d"},
        {"address": "/dns/lhr-tnt-ssfn-00.testnet.sui.io/udp/8084", "peer-id": "9393d6056bb9c9d8475a3cf3525c747257f17c6a698a7062cbbd1875bc6ef71e"},
        {"address": "/dns/mel-tnt-ssfn-00.testnet.sui.io/udp/8084", "peer-id": "c88742f46e66a11cb8c84aca488065661401ef66f726cb9afeb8a5786d83456e"},
    ],
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
        """Return seed peer hosts from MystenLabs docs.

        Tries to fetch fresh data from GitHub MDX docs. On failure, falls back
        to hardcoded _FALLBACK_SEED_PEERS (Mysten-operated SSFN infrastructure).
        Returns host dicts with discovery_method='seed_peer', confidence=0.80.
        """
        raw_peers = await self._fetch_seed_peers_from_github(network)
        if not raw_peers:
            raw_peers = _FALLBACK_SEED_PEERS.get(network, _FALLBACK_SEED_PEERS.get("sui", []))

        return self._parse_seed_peer_entries(raw_peers, network)

    async def _fetch_seed_peers_from_github(self, network: str) -> list[dict]:
        """Try to fetch and parse seed peers from the MystenLabs GitHub docs MDX."""
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(SUI_SEED_PEER_URL)
            response.raise_for_status()
        except Exception:
            logger.warning("Failed to fetch seed peers from %s", SUI_SEED_PEER_URL)
            return []

        # Extract YAML blocks from MDX — look for seed-peers sections
        return _extract_seed_peers_from_mdx(response.text, network)

    def _parse_seed_peer_entries(self, entries: list[dict], network: str) -> list[dict]:
        """Convert raw seed peer dicts (with 'address' key) to host dicts."""
        hosts = []
        for entry in entries:
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
                "reasoning": f"Seed peer from MystenLabs config ({network})",
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


def _extract_seed_peers_from_mdx(text: str, network: str) -> list[dict]:
    """Extract seed-peers from the Sui full-node MDX docs.

    The MDX contains fenced YAML blocks with seed-peers for mainnet/testnet.
    We find the right block by looking for network-specific hostnames.
    """
    # Determine which hostname pattern to look for
    if "testnet" in network:
        marker = "testnet.sui.io"
    elif "devnet" in network:
        marker = "devnet.sui.io"
    else:
        marker = "mainnet.sui.io"

    # Extract all fenced YAML code blocks
    blocks = re.findall(r"```yaml\s*\n(.*?)```", text, re.DOTALL)
    for block in blocks:
        if marker not in block:
            continue
        try:
            data = yaml.safe_load(block)
        except yaml.YAMLError:
            continue
        peers = (data or {}).get("p2p-config", {}).get("seed-peers", [])
        if peers:
            return peers
    return []
