"""
In-memory discovery store — drop-in replacement for DiscoveryApiClient.

Use with ``--local`` / ``--output`` CLI flags to run ferret without a
running discovery-service backend.  The LLM gateway is still required.
"""

from datetime import datetime, timezone
from uuid import UUID, uuid4


class LocalDiscoveryStore:
    """Stores discovery results in-memory; no HTTP calls."""

    def __init__(self) -> None:
        self._runs: dict[UUID, dict] = {}
        self._hosts: dict[UUID, dict] = {}
        self._validators: dict[UUID, dict] = {}
        # Dedup indexes
        self._host_by_ip_port: dict[tuple, UUID] = {}
        self._host_by_hostname_port: dict[tuple, UUID] = {}
        self._validator_by_pubkey: dict[str, UUID] = {}
        # Current run tracking
        self._current_run_id: UUID | None = None
        self._network: str | None = None

    # ── Discovery Runs ────────────────────────────────────────────────────────

    async def create_discovery_run(self, network_name: str) -> object:
        """Create a new in-memory discovery run."""
        from src.db import DiscoveryRun

        run_id = uuid4()
        now = datetime.now(timezone.utc)
        self._runs[run_id] = {
            "id": run_id,
            "network_name": network_name,
            "started_at": now,
            "status": "running",
            "tool_calls": 0,
            "tokens": 0,
            "hosts_new": 0,
            "hosts_updated": 0,
            "hosts_gone": 0,
            "summary": None,
            "completed_at": None,
        }
        self._current_run_id = run_id
        self._network = network_name
        return DiscoveryRun(
            id=run_id,
            network_name=network_name,
            started_at=now,
            status="running",
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
        run = self._runs.get(run_id)
        if run is None:
            return
        if tool_calls is not None:
            run["tool_calls"] += tool_calls
        if tokens is not None:
            run["tokens"] += tokens
        if hosts_new is not None:
            run["hosts_new"] += hosts_new
        if hosts_updated is not None:
            run["hosts_updated"] += hosts_updated
        if hosts_gone is not None:
            run["hosts_gone"] += hosts_gone

    async def complete_discovery_run(
        self,
        run_id: UUID,
        *,
        summary: str | None = None,
        transcript: list | None = None,
        status: str = "completed",
    ) -> None:
        run = self._runs.get(run_id)
        if run is None:
            return
        run["status"] = status
        run["summary"] = summary
        run["completed_at"] = datetime.now(timezone.utc)

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
        # Dedup: prefer (ip, port) key; fall back to (hostname, port)
        existing_id: UUID | None = None
        if ip_address is not None:
            existing_id = self._host_by_ip_port.get((ip_address, port))
        if existing_id is None and hostname is not None:
            existing_id = self._host_by_hostname_port.get((hostname, port))

        if existing_id is not None:
            host = self._hosts[existing_id]
            # Update mutable fields
            if ip_address is not None:
                host["ip_address"] = ip_address
            if hostname is not None:
                host["hostname"] = hostname
            if service_type is not None:
                host["service_type"] = service_type
            if protocol is not None:
                host["protocol"] = protocol
            if discovery_method is not None:
                host["discovery_method"] = discovery_method
            if validator_id is not None:
                host["validator_id"] = validator_id
            if metadata is not None:
                host["metadata"] = metadata
            host["confidence"] = confidence
            host["last_seen_at"] = datetime.now(timezone.utc)
            return existing_id, False

        host_id = uuid4()
        now = datetime.now(timezone.utc)
        self._hosts[host_id] = {
            "id": host_id,
            "network_name": network_name,
            "ip_address": ip_address,
            "port": port,
            "protocol": protocol,
            "service_type": service_type,
            "hostname": hostname,
            "confidence": confidence,
            "discovery_method": discovery_method,
            "validator_id": validator_id,
            "metadata": metadata or {},
            "is_active": True,
            "created_at": now,
            "last_seen_at": now,
        }
        if ip_address is not None:
            self._host_by_ip_port[(ip_address, port)] = host_id
        if hostname is not None:
            self._host_by_hostname_port[(hostname, port)] = host_id
        return host_id, True

    async def flag_host_gone(self, host_id: UUID, reason: str) -> bool:
        host = self._hosts.get(host_id)
        if host is None:
            return False
        host["is_active"] = False
        host["gone_reason"] = reason
        return True

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
        results = []
        for host in self._hosts.values():
            if host["network_name"] != network_name:
                continue
            if is_active is not None and host["is_active"] != is_active:
                continue
            if service_type is not None and host.get("service_type") != service_type:
                continue
            if min_confidence is not None and host["confidence"] < min_confidence:
                continue
            if not_seen_since is not None:
                last_seen = host.get("last_seen_at")
                if last_seen is None or last_seen > not_seen_since:
                    continue
            results.append(dict(host))
            if len(results) >= limit:
                break
        return results

    # ── Validators ────────────────────────────────────────────────────────────

    async def get_validators(self, network_name: str) -> list[dict]:
        return [
            dict(v)
            for v in self._validators.values()
            if v["network_name"] == network_name
        ]

    async def get_or_create_validator(
        self, network_name: str, pubkey: str, operator_name: str | None = None
    ) -> UUID:
        existing_id = self._validator_by_pubkey.get(pubkey)
        if existing_id is not None:
            return existing_id

        validator_id = uuid4()
        self._validators[validator_id] = {
            "id": validator_id,
            "network_name": network_name,
            "pubkey": pubkey,
            "operator_name": operator_name,
        }
        self._validator_by_pubkey[pubkey] = validator_id
        return validator_id

    # ── No-op / empty stubs ───────────────────────────────────────────────────

    async def get_recent_runs(self, network_name: str, limit: int = 5) -> list[dict]:
        return []

    async def get_discovery_diff(self, network_name: str, since: datetime) -> dict:
        return {}

    async def search_hypotheses(
        self,
        embedding: list[float],
        min_success_rate: float | None = None,
        limit: int = 10,
    ) -> list[dict]:
        return []

    async def save_hypothesis(self, *args, **kwargs) -> UUID:
        return uuid4()

    async def close(self) -> None:
        pass

    # ── Results export ────────────────────────────────────────────────────────

    def get_results(self) -> dict:
        """Return a JSON-serialisable summary of this run's findings."""
        run_id = self._current_run_id
        run = self._runs.get(run_id) if run_id else {}
        network = self._network or ""

        hosts = [
            {
                k: (str(v) if isinstance(v, UUID) else
                    v.isoformat() if isinstance(v, datetime) else v)
                for k, v in host.items()
            }
            for host in self._hosts.values()
            if host["network_name"] == network
        ]

        validators = [
            {
                k: (str(v) if isinstance(v, UUID) else v)
                for k, v in v.items()
            }
            for v in self._validators.values()
            if v["network_name"] == network
        ]

        started_at = run.get("started_at")
        completed_at = run.get("completed_at")

        return {
            "network": network,
            "run_id": str(run_id) if run_id else None,
            "started_at": started_at.isoformat() if started_at else None,
            "completed_at": completed_at.isoformat() if completed_at else None,
            "status": run.get("status"),
            "stats": {
                "hosts_new": run.get("hosts_new", 0),
                "hosts_updated": run.get("hosts_updated", 0),
                "hosts_gone": run.get("hosts_gone", 0),
                "tool_calls": run.get("tool_calls", 0),
                "tokens": run.get("tokens", 0),
            },
            "hosts": hosts,
            "validators": validators,
            "summary": run.get("summary"),
        }
