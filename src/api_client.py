"""
HTTP client for discovery-service.

Network names are used directly in all API calls — no UUID lookups needed.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

import httpx

from src.db import DiscoveryRun, DiscoveryRunResult


class DiscoveryApiClient:
    """HTTP client wrapping the discovery-service REST API."""

    def __init__(self, base_url: str, timeout: float = 30.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(base_url=self._base_url, timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()

    # ── Discovery Runs ────────────────────────────────────────────────────────

    async def create_discovery_run(self, network_name: str) -> DiscoveryRun:
        """Create a new discovery run and return it."""
        resp = await self._client.post("/runs", json={"network_name": network_name})
        resp.raise_for_status()
        data = resp.json()
        return DiscoveryRun(
            id=UUID(data["run_id"]),
            network_name=data.get("network_name", network_name),
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
        network_name: str,
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
            "network_name": network_name,
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
        network_name: str,
        *,
        operator_name: str | None = None,
        service_type: str | None = None,
        min_confidence: float | None = None,
        is_active: bool | None = None,
        not_seen_since: datetime | None = None,
        limit: int = 500,
    ) -> list[dict]:
        """Query hosts with optional filters."""
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

        resp = await self._client.get(f"/networks/{network_name}/hosts", params=params)
        resp.raise_for_status()
        return resp.json()

    # ── Validators ────────────────────────────────────────────────────────────

    async def get_validators(self, network_name: str) -> list[dict]:
        """Return all validators for a network."""
        resp = await self._client.get(f"/networks/{network_name}/validators")
        resp.raise_for_status()
        return resp.json().get("validators", [])

    async def get_or_create_validator(
        self, network_name: str, pubkey: str, operator_name: str | None = None
    ) -> UUID:
        """Get or create a validator."""
        body: dict[str, Any] = {"network_name": network_name, "pubkey": pubkey}
        if operator_name is not None:
            body["operator_name"] = operator_name
        resp = await self._client.post("/validators", json=body)
        resp.raise_for_status()
        return UUID(resp.json()["id"])

    # ── Hypotheses + Diff ─────────────────────────────────────────────────────

    async def get_discovery_diff(
        self, network_name: str, since: datetime
    ) -> dict:
        """Return diff since the given timestamp."""
        resp = await self._client.get(
            f"/networks/{network_name}/diff",
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

    async def get_recent_runs(self, network_name: str, limit: int = 5) -> list[dict]:
        """Return recent discovery runs for a network."""
        resp = await self._client.get(
            f"/networks/{network_name}/runs",
            params={"limit": limit},
        )
        resp.raise_for_status()
        return resp.json()
