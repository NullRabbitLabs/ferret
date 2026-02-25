"""
State management tools: thin wrappers around Database methods.

These tools let the agent read and write to the discovery inventory.
"""

import ipaddress
from datetime import datetime, timezone
from uuid import UUID

from src.db import Database
from src.tools.schemas import (
    BULK_REPORT_DISCOVERED_HOSTS_SCHEMA,
    FLAG_HOST_GONE_SCHEMA,
    GET_DISCOVERY_DIFF_SCHEMA,
    GET_KNOWN_HOSTS_SCHEMA,
    GET_KNOWN_VALIDATORS_SCHEMA,
    REPORT_DISCOVERED_HOST_SCHEMA,
    SEARCH_PAST_HYPOTHESES_SCHEMA,
)

# CDN and proxy IP ranges — these are not validator infrastructure
_CDN_RANGES = [
    # Cloudflare
    ipaddress.ip_network("104.16.0.0/12"),
    ipaddress.ip_network("172.64.0.0/13"),
    # Fastly
    ipaddress.ip_network("151.101.0.0/16"),
    # Akamai (selected ranges)
    ipaddress.ip_network("23.32.0.0/11"),
    ipaddress.ip_network("96.16.0.0/15"),
]


def _is_cdn_ip(ip_str: str) -> bool:
    """Return True if ip_str falls within a known CDN/proxy range."""
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return any(addr in net for net in _CDN_RANGES)


class StateTools:
    """
    State management tools that wrap Database operations.

    Unlike network tools, these are not subclasses of BaseTool (no rate limiting
    needed — they're local DB calls).
    """

    def __init__(self, db: Database, gateway_client=None) -> None:
        self._db = db
        self._gateway = gateway_client  # for get_embedding (search_past_hypotheses)
        self._run_stats: dict[str, dict] = {}  # run_id -> {hosts_new, hosts_updated, ...}

    def init_run_stats(self, run_id: str) -> None:
        self._run_stats[run_id] = {
            "hosts_new": 0,
            "hosts_updated": 0,
            "hosts_gone": 0,
        }

    def get_run_stats(self, run_id: str) -> dict:
        return self._run_stats.get(run_id, {})

    def get_tool_map(self) -> dict:
        return {
            "get_known_hosts": self.get_known_hosts,
            "get_known_validators": self.get_known_validators,
            "bulk_report_discovered_hosts": self.bulk_report_discovered_hosts,
            "report_discovered_host": self.report_discovered_host,
            "flag_host_gone": self.flag_host_gone,
            "search_past_hypotheses": self.search_past_hypotheses,
            "get_discovery_diff": self.get_discovery_diff,
        }

    @staticmethod
    def schemas() -> list[dict]:
        return [
            GET_KNOWN_HOSTS_SCHEMA,
            GET_KNOWN_VALIDATORS_SCHEMA,
            REPORT_DISCOVERED_HOST_SCHEMA,
            FLAG_HOST_GONE_SCHEMA,
            SEARCH_PAST_HYPOTHESES_SCHEMA,
            GET_DISCOVERY_DIFF_SCHEMA,
        ]

    _GET_KNOWN_HOSTS_DEFAULT_LIMIT = 50
    _GET_KNOWN_HOSTS_MAX_LIMIT = 200
    _GET_KNOWN_HOSTS_COUNT_THRESHOLD = 20

    def _has_meaningful_filters(self, filters: dict) -> bool:
        """Return True if filters contain at least one meaningful narrowing condition."""
        return bool(
            filters.get("operator_name")
            or filters.get("service_type")
            or filters.get("min_confidence") is not None
            or filters.get("is_active") is not None
            or filters.get("not_seen_since")
        )

    async def get_known_hosts(
        self,
        network: str,
        filters: dict | None = None,
        limit: int | None = None,
        run_id: str | None = None,
        **kwargs,
    ) -> dict:
        network_id = await self._db.get_network_id(network)
        if not network_id:
            return {"error": f"Unknown network: {network!r}"}

        filters = filters or {}
        not_seen_since = None
        if "not_seen_since" in filters:
            try:
                not_seen_since = datetime.fromisoformat(filters["not_seen_since"])
            except ValueError:
                return {"error": f"Invalid not_seen_since format: {filters['not_seen_since']!r}"}

        hosts = await self._db.get_hosts(
            network_id,
            operator_name=filters.get("operator_name"),
            service_type=filters.get("service_type"),
            min_confidence=filters.get("min_confidence"),
            is_active=filters.get("is_active"),
            not_seen_since=not_seen_since,
        )
        total = len(hosts)

        # Large unfiltered result: return a count summary to save context budget
        if total > self._GET_KNOWN_HOSTS_COUNT_THRESHOLD and not self._has_meaningful_filters(filters):
            service_counts: dict[str, int] = {}
            for h in hosts:
                stype = h.get("service_type") or "unknown"
                service_counts[stype] = service_counts.get(stype, 0) + 1
            return {
                "network": network,
                "count": total,
                "by_service_type": service_counts,
                "note": (
                    f"Large inventory ({total} hosts). Use filters (operator_name, service_type) "
                    "or get_known_validators to narrow results."
                ),
            }

        effective_limit = min(
            limit if limit is not None else self._GET_KNOWN_HOSTS_DEFAULT_LIMIT,
            self._GET_KNOWN_HOSTS_MAX_LIMIT,
        )
        hosts = hosts[:effective_limit]

        # Return compact summaries — strip metadata/timestamps to keep context small
        compact = [
            {
                "ip_address": str(h["ip_address"]) if h.get("ip_address") else None,
                "hostname": h.get("hostname"),
                "port": h.get("port"),
                "service_type": h.get("service_type"),
                "confidence": h.get("confidence"),
                "last_seen": str(h.get("last_seen_at", ""))[:10],
            }
            for h in hosts
        ]
        result: dict = {"network": network, "count": total, "hosts": compact}
        if total > effective_limit:
            result["note"] = f"showing {effective_limit} of {total} — use filters or increase limit to see more"
        return result

    async def get_known_validators(
        self, network: str, run_id: str | None = None, **kwargs
    ) -> dict:
        network_id = await self._db.get_network_id(network)
        if not network_id:
            return {"error": f"Unknown network: {network!r}"}

        validators = await self._db.get_validators(network_id)
        # Return compact summaries — full pubkeys are too long for context
        compact = [
            {
                "pubkey": v["pubkey"][:16] + "..." if v.get("pubkey") else None,
                "operator_name": v.get("operator_name"),
                "host_count": v.get("host_count", 0),
                "active_host_count": v.get("active_host_count", 0),
            }
            for v in validators
        ]
        return {"network": network, "count": len(compact), "validators": compact}

    async def report_discovered_host(
        self,
        network: str,
        ip_address: str,
        service_type: str,
        confidence: float,
        discovery_method: str,
        reasoning: str,
        port: int | None = None,
        protocol: str | None = None,
        validator_pubkey: str | None = None,
        hostname: str | None = None,
        run_id: str | None = None,
        **kwargs,
    ) -> dict:
        # Reject CDN/proxy IPs before touching the database
        if ip_address and _is_cdn_ip(ip_address):
            return {
                "error": (
                    f"Rejected: {ip_address} is a CDN/proxy IP. "
                    "Not validator infrastructure."
                ),
                "ip_address": ip_address,
            }

        network_id = await self._db.get_network_id(network)
        if not network_id:
            return {"error": f"Unknown network: {network!r}"}

        validator_id: UUID | None = None
        if validator_pubkey:
            validator_id = await self._db.get_or_create_validator(network_id, validator_pubkey)

        host_id, is_new = await self._db.upsert_host(
            network_id,
            ip_address,
            port,
            protocol=protocol,
            service_type=service_type,
            hostname=hostname,
            confidence=confidence,
            discovery_method=discovery_method,
            validator_id=validator_id,
            metadata={"reasoning": reasoning},
        )

        if run_id and run_id in self._run_stats:
            if is_new:
                self._run_stats[run_id]["hosts_new"] += 1
            else:
                self._run_stats[run_id]["hosts_updated"] += 1

        return {
            "id": str(host_id),
            "is_new": is_new,
            "ip_address": ip_address,
            "port": port,
        }

    async def bulk_report_discovered_hosts(
        self,
        network: str,
        hosts: list[dict],
        run_id: str | None = None,
        **kwargs,
    ) -> dict:
        """Import multiple hosts in a single call. Use for on-chain bulk imports."""
        network_id = await self._db.get_network_id(network)
        if not network_id:
            return {"error": f"Unknown network: {network!r}"}

        total_new = 0
        total_updated = 0
        errors: list[dict] = []

        for h in hosts:
            ip = h.get("ip_address")
            hostname = h.get("hostname")
            if not ip and not hostname:
                continue
            if ip and _is_cdn_ip(ip):
                errors.append({"host": ip, "error": "Skipped: CDN/proxy IP"})
                continue

            validator_id = None
            if h.get("validator_pubkey"):
                validator_id = await self._db.get_or_create_validator(
                    network_id,
                    h["validator_pubkey"],
                    operator_name=h.get("operator_name"),
                )

            try:
                host_id, is_new = await self._db.upsert_host(
                    network_id,
                    ip,
                    h.get("port"),
                    protocol=h.get("protocol"),
                    service_type=h.get("service_type", "unknown"),
                    hostname=hostname,
                    confidence=h.get("confidence", 0.5),
                    discovery_method=h.get("discovery_method", "bulk_import"),
                    validator_id=validator_id,
                    metadata={"reasoning": h.get("reasoning", "")},
                )
                if is_new:
                    total_new += 1
                else:
                    total_updated += 1
            except Exception as e:
                key = ip or hostname
                errors.append({"host": key, "error": str(e)})

        if run_id and run_id in self._run_stats:
            self._run_stats[run_id]["hosts_new"] += total_new
            self._run_stats[run_id]["hosts_updated"] += total_updated

        return {
            "total": len(hosts),
            "new": total_new,
            "updated": total_updated,
            "errors": errors,
        }

    async def flag_host_gone(
        self,
        host_id: str,
        reason: str,
        run_id: str | None = None,
        **kwargs,
    ) -> dict:
        try:
            uid = UUID(host_id)
        except ValueError:
            return {"error": f"Invalid host_id: {host_id!r}"}

        updated = await self._db.flag_host_gone(uid, reason)

        if updated and run_id and run_id in self._run_stats:
            self._run_stats[run_id]["hosts_gone"] += 1

        return {"host_id": host_id, "updated": updated}

    async def search_past_hypotheses(
        self,
        query: str,
        min_success_rate: float | None = None,
        run_id: str | None = None,
        **kwargs,
    ) -> dict:
        if not self._gateway:
            return {
                "query": query,
                "results": [],
                "note": "Gateway not configured — hypothesis search unavailable",
            }

        try:
            embedding = await self._gateway.get_embedding(query)
        except Exception as e:
            return {"query": query, "results": [], "error": f"Embedding failed: {e}"}

        results = await self._db.search_hypotheses(embedding, min_success_rate=min_success_rate)
        return {"query": query, "results": results}

    async def get_discovery_diff(
        self,
        network: str,
        since: str,
        run_id: str | None = None,
        **kwargs,
    ) -> dict:
        network_id = await self._db.get_network_id(network)
        if not network_id:
            return {"error": f"Unknown network: {network!r}"}

        try:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError:
            return {"error": f"Invalid 'since' datetime: {since!r}"}

        diff = await self._db.get_discovery_diff(network_id, since_dt)
        return diff
