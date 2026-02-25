"""
HTTP client for discovery-service.

Provides the same interface as the old Database class so agent.py and state.py
need only import path changes — no logic changes.

The service API uses network names in URLs (e.g. /networks/{name}/hosts) but
the Database interface uses UUIDs. This client caches UUID→name mappings
from get_network() / get_network_id() calls, which always precede UUID-based calls.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

import httpx

from src.db import DiscoveryRun, DiscoveryRunResult


class DiscoveryApiClient:
    """
    HTTP client wrapping the discovery-service REST API.

    Same method signatures as the old Database class.
    """

    def __init__(self, base_url: str, timeout: float = 30.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(base_url=self._base_url, timeout=timeout)
        self._name_cache: dict[UUID, str] = {}  # network_id → network_name

    async def close(self) -> None:
        await self._client.aclose()

    # ── Network ──────────────────────────────────────────────────────────────

    async def get_network_id(self, name: str) -> UUID | None:
        """Look up a network UUID by name."""
        resp = await self._client.get(f"/networks/{name}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        uid = UUID(str(data["id"]))
        self._name_cache[uid] = name
        return uid

    async def get_network(self, name: str) -> dict | None:
        """Return full network record as dict."""
        resp = await self._client.get(f"/networks/{name}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        uid = UUID(str(data["id"]))
        data["id"] = uid
        self._name_cache[uid] = name
        return data

    def _name(self, network_id: UUID) -> str:
        """Resolve network name from cache (populated by prior get_network* calls)."""
        name = self._name_cache.get(network_id)
        if not name:
            raise RuntimeError(
                f"Network name for {network_id} not in cache. "
                "Call get_network() or get_network_id() first."
            )
        return name

    # ── Discovery Runs ────────────────────────────────────────────────────────

    async def create_discovery_run(self, network_id: UUID) -> DiscoveryRun:
        """Create a new discovery run and return it."""
        resp = await self._client.post("/runs", json={"network_id": str(network_id)})
        resp.raise_for_status()
        data = resp.json()
        return DiscoveryRun(
            id=UUID(data["run_id"]),
            network_id=UUID(data["network_id"]),
            started_at=datetime.fromisoformat(data["started_at"]),
            status=data["status"],
        )

    async def update_run_stats(
        self,
        run_id: UUID,
        *,
        tool_calls: int | None = None,
        tokens: int | None = None,
        hosts_new: int | None = None,
        hosts_updated: int | None = None,
        hosts_gone: int | None = None,
    ) -> None:
        """Increment run stats counters."""
        body: dict[str, Any] = {}
        if tool_calls is not None:
            body["tool_calls"] = tool_calls
        if tokens is not None:
            body["tokens"] = tokens
        if hosts_new is not None:
            body["hosts_new"] = hosts_new
        if hosts_updated is not None:
            body["hosts_updated"] = hosts_updated
        if hosts_gone is not None:
            body["hosts_gone"] = hosts_gone
        resp = await self._client.patch(f"/runs/{run_id}", json=body)
        resp.raise_for_status()

    async def complete_discovery_run(
        self,
        run_id: UUID,
        *,
        summary: str | None = None,
        transcript: list | None = None,
        status: str = "completed",
    ) -> None:
        """Mark a run as complete."""
        body: dict[str, Any] = {"status": status}
        if summary is not None:
            body["summary"] = summary
        if transcript is not None:
            body["transcript"] = transcript
        resp = await self._client.post(f"/runs/{run_id}/complete", json=body)
        resp.raise_for_status()

    # ── Hosts ─────────────────────────────────────────────────────────────────

    async def upsert_host(
        self,
        network_id: UUID,
        ip_address: str | None,
        port: int | None,
        *,
        protocol: str | None = None,
        service_type: str | None = None,
        hostname: str | None = None,
        confidence: float = 0.5,
        discovery_method: str | None = None,
        validator_id: UUID | None = None,
        metadata: dict | None = None,
    ) -> tuple[UUID, bool]:
        """Upsert a host. Returns (host_id, is_new)."""
        body: dict[str, Any] = {
            "network_id": str(network_id),
            "confidence": confidence,
        }
        if ip_address is not None:
            body["ip_address"] = ip_address
        if hostname is not None:
            body["hostname"] = hostname
        if port is not None:
            body["port"] = port
        if protocol is not None:
            body["protocol"] = protocol
        if service_type is not None:
            body["service_type"] = service_type
        if discovery_method is not None:
            body["discovery_method"] = discovery_method
        if validator_id is not None:
            body["validator_id"] = str(validator_id)
        if metadata is not None:
            body["metadata"] = metadata

        resp = await self._client.post("/hosts", json=body)
        resp.raise_for_status()
        data = resp.json()
        return UUID(data["id"]), data["is_new"]

    async def flag_host_gone(self, host_id: UUID, reason: str) -> bool:
        """Mark a host as inactive."""
        resp = await self._client.patch(
            f"/hosts/{host_id}/gone",
            json={"reason": reason},
        )
        resp.raise_for_status()
        return resp.json()["updated"]

    async def get_hosts(
        self,
        network_id: UUID,
        *,
        operator_name: str | None = None,
        service_type: str | None = None,
        min_confidence: float | None = None,
        is_active: bool | None = None,
        not_seen_since: datetime | None = None,
        limit: int = 500,
    ) -> list[dict]:
        """Query hosts with optional filters."""
        name = self._name(network_id)
        params: dict[str, Any] = {"limit": limit}
        if operator_name is not None:
            params["operator_name"] = operator_name
        if service_type is not None:
            params["service_type"] = service_type
        if min_confidence is not None:
            params["min_confidence"] = min_confidence
        if is_active is not None:
            params["is_active"] = str(is_active).lower()
        if not_seen_since is not None:
            params["not_seen_since"] = not_seen_since.isoformat()

        resp = await self._client.get(f"/networks/{name}/hosts", params=params)
        resp.raise_for_status()
        return resp.json()

    # ── Validators ────────────────────────────────────────────────────────────

    async def get_validators(self, network_id: UUID) -> list[dict]:
        """Return all validators for a network."""
        name = self._name(network_id)
        resp = await self._client.get(f"/networks/{name}/validators")
        resp.raise_for_status()
        return resp.json().get("validators", [])

    async def get_or_create_validator(
        self, network_id: UUID, pubkey: str, operator_name: str | None = None
    ) -> UUID:
        """Get or create a validator."""
        body: dict[str, Any] = {"network_id": str(network_id), "pubkey": pubkey}
        if operator_name is not None:
            body["operator_name"] = operator_name
        resp = await self._client.post("/validators", json=body)
        resp.raise_for_status()
        return UUID(resp.json()["id"])

    # ── Hypotheses + Diff ─────────────────────────────────────────────────────

    async def get_discovery_diff(
        self, network_id: UUID, since: datetime
    ) -> dict:
        """Return diff since the given timestamp."""
        name = self._name(network_id)
        resp = await self._client.get(
            f"/networks/{name}/diff",
            params={"since": since.isoformat()},
        )
        resp.raise_for_status()
        return resp.json()

    async def save_hypothesis(
        self,
        run_id: UUID,
        hypothesis: str,
        method: str,
        confidence_before: float,
        embedding: list[float] | None = None,
    ) -> UUID:
        """Save a discovery hypothesis."""
        body: dict[str, Any] = {
            "run_id": str(run_id),
            "hypothesis": hypothesis,
            "method": method,
            "confidence_before": confidence_before,
        }
        if embedding is not None:
            body["embedding"] = embedding
        resp = await self._client.post("/hypotheses", json=body)
        resp.raise_for_status()
        return UUID(resp.json()["id"])

    async def search_hypotheses(
        self,
        embedding: list[float],
        min_success_rate: float | None = None,
        limit: int = 10,
    ) -> list[dict]:
        """Search past hypotheses by vector similarity."""
        body: dict[str, Any] = {"embedding": embedding, "limit": limit}
        if min_success_rate is not None:
            body["min_success_rate"] = min_success_rate
        resp = await self._client.post("/hypotheses/search", json=body)
        resp.raise_for_status()
        return resp.json().get("results", [])

    async def get_recent_runs(self, network_id: UUID, limit: int = 5) -> list[dict]:
        """Return recent discovery runs for a network."""
        name = self._name(network_id)
        resp = await self._client.get(
            f"/networks/{name}/runs",
            params={"limit": limit},
        )
        resp.raise_for_status()
        return resp.json()
