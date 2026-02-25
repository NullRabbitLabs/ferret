"""
Solana blockchain tool implementations.

Queries the Solana JSON-RPC API for cluster node and validator information.
Results are cached for 1 hour.
"""

import time
from collections.abc import Callable

import httpx

from src.tools.blockchain.base import ChainTools

SOLANA_GET_CLUSTER_NODES_SCHEMA = {
    "type": "function",
    "function": {
        "name": "solana_get_cluster_nodes",
        "description": (
            "Fetch all known cluster nodes from the Solana network. "
            "Returns pubkey, gossip address, tpu, rpc, version, and feature_set for each node. "
            "Results cached for 1 hour."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}

SOLANA_GET_VOTE_ACCOUNTS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "solana_get_vote_accounts",
        "description": (
            "Fetch current and delinquent vote accounts from the Solana network. "
            "Returns pubkey, node_pubkey, activated_stake, commission, "
            "last_vote, and root_slot for each account."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}


class SolanaTools(ChainTools):
    """Solana blockchain discovery tools."""

    def __init__(self, rpc_url: str, cache_ttl: int = 3600) -> None:
        self._rpc_url = rpc_url
        self._cache_ttl = cache_ttl
        self._cluster_nodes_cache: dict | None = None
        self._cluster_nodes_cached_at: float = 0.0

    def schemas(self) -> list[dict]:
        return [
            SOLANA_GET_CLUSTER_NODES_SCHEMA,
            SOLANA_GET_VOTE_ACCOUNTS_SCHEMA,
        ]

    def primary_tool_name(self) -> str:
        return "solana_get_cluster_nodes"

    async def get_seed_hosts(self, network: str) -> list[dict]:
        """Fetch cluster nodes and return host list for bulk import.

        Deduplicates by (ip, port) so the same IP on different ports
        (gossip:8001, rpc:8899) are both recorded as separate hosts.
        """
        result = await self.get_cluster_nodes()
        hosts = []
        for node in result.get("nodes", []):
            pubkey = node.get("pubkey", "")
            label = pubkey[:10] if pubkey else "unknown"
            seen: set[tuple[str, int]] = set()
            for field, service_type in [("gossip", "gossip"), ("rpc", "rpc"), ("tpu", "tpu")]:
                addr = node.get(field)
                if not addr:
                    continue
                try:
                    ip, port_str = addr.rsplit(":", 1)
                    port = int(port_str)
                except (ValueError, AttributeError):
                    continue
                key = (ip, port)
                if key in seen:
                    continue
                seen.add(key)
                hosts.append({
                    "ip_address": ip,
                    "port": port,
                    "service_type": service_type,
                    "confidence": 0.95,
                    "discovery_method": "on_chain",
                    "validator_pubkey": pubkey,
                    "reasoning": f"On-chain {field} address for {label}",
                })
        return hosts

    def get_tool_map(self) -> dict[str, Callable]:
        return {
            "solana_get_cluster_nodes": self.get_cluster_nodes,
            "solana_get_vote_accounts": self.get_vote_accounts,
        }

    def _is_cluster_nodes_cache_valid(self) -> bool:
        return (
            self._cluster_nodes_cache is not None
            and (time.monotonic() - self._cluster_nodes_cached_at) < self._cache_ttl
        )

    async def _rpc_call(self, method: str, params: list | None = None) -> dict:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                self._rpc_url,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": method,
                    "params": params or [],
                },
            )
        response.raise_for_status()
        return response.json()

    async def get_cluster_nodes(self, **kwargs) -> dict:
        """Fetch cluster nodes from getClusterNodes."""
        if self._is_cluster_nodes_cache_valid():
            return self._cluster_nodes_cache  # type: ignore[return-value]

        data = await self._rpc_call("getClusterNodes")
        nodes_raw = data.get("result", [])
        nodes = [
            {
                "pubkey": n.get("pubkey"),
                "gossip": n.get("gossip"),
                "tpu": n.get("tpu"),
                "rpc": n.get("rpc"),
                "version": n.get("version"),
                "feature_set": n.get("featureSet"),
            }
            for n in nodes_raw
        ]

        result = {"nodes": nodes, "count": len(nodes)}
        self._cluster_nodes_cache = result
        self._cluster_nodes_cached_at = time.monotonic()
        return result

    async def get_vote_accounts(self, **kwargs) -> dict:
        """Fetch vote accounts from getVoteAccounts."""
        data = await self._rpc_call("getVoteAccounts")
        result = data.get("result", {})

        def parse_accounts(accounts: list) -> list:
            return [
                {
                    "pubkey": a.get("votePubkey"),
                    "node_pubkey": a.get("nodePubkey"),
                    "activated_stake": a.get("activatedStake"),
                    "commission": a.get("commission"),
                    "last_vote": a.get("lastVote"),
                    "root_slot": a.get("rootSlot"),
                }
                for a in accounts
            ]

        return {
            "current": parse_accounts(result.get("current", [])),
            "delinquent": parse_accounts(result.get("delinquent", [])),
        }
