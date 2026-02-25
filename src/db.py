"""
Database client for the discovery agent.

Uses asyncpg with a connection pool. Provides typed methods for all
discovery-agent database operations.
"""

import json
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg


@dataclass
class DiscoveryRun:
    id: UUID
    network_id: UUID
    started_at: datetime
    status: str = "running"
    hosts_discovered: int = 0
    hosts_new: int = 0
    hosts_updated: int = 0
    hosts_gone: int = 0
    tool_calls: int = 0
    llm_tokens_used: int = 0


@dataclass
class DiscoveryRunResult:
    run_id: UUID
    network: str
    hosts_discovered: int
    hosts_new: int
    hosts_updated: int
    hosts_gone: int
    tool_calls: int
    llm_tokens_used: int
    summary: str | None = None


class Database:
    """Async PostgreSQL client for discovery agent operations."""

    def __init__(self, dsn: str, min_size: int = 1, max_size: int = 5) -> None:
        self._dsn = dsn
        self._min_size = min_size
        self._max_size = max_size
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        if self._pool is not None:
            return
        self._pool = await asyncpg.create_pool(
            dsn=self._dsn,
            min_size=self._min_size,
            max_size=self._max_size,
            command_timeout=60,
        )

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @asynccontextmanager
    async def acquire(self):
        if self._pool is None:
            raise RuntimeError("Database not connected. Call connect() first.")
        async with self._pool.acquire() as conn:
            yield conn

    async def get_network_id(self, name: str) -> UUID | None:
        """Look up a network UUID by name."""
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id FROM discovery.networks WHERE name = $1", name
            )
        return row["id"] if row else None

    async def get_network(self, name: str) -> dict | None:
        """Return full network record as dict."""
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, name, chain_type, rpc_endpoints, enabled, discovery_config "
                "FROM discovery.networks WHERE name = $1",
                name,
            )
        if not row:
            return None
        return dict(row)

    async def create_discovery_run(self, network_id: UUID) -> DiscoveryRun:
        """Insert a new discovery_run record and return it."""
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO discovery.discovery_runs (network_id, status)
                VALUES ($1, 'running')
                RETURNING id, network_id, started_at, status
                """,
                network_id,
            )
        return DiscoveryRun(
            id=row["id"],
            network_id=row["network_id"],
            started_at=row["started_at"],
            status=row["status"],
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
        sets = []
        args: list[Any] = []
        idx = 1

        if tool_calls is not None:
            sets.append(f"tool_calls = ${ idx}")
            args.append(tool_calls)
            idx += 1
        if tokens is not None:
            sets.append(f"llm_tokens_used = ${idx}")
            args.append(tokens)
            idx += 1
        if hosts_new is not None:
            sets.append(f"hosts_new = ${idx}")
            args.append(hosts_new)
            idx += 1
        if hosts_updated is not None:
            sets.append(f"hosts_updated = ${idx}")
            args.append(hosts_updated)
            idx += 1
        if hosts_gone is not None:
            sets.append(f"hosts_gone = ${idx}")
            args.append(hosts_gone)
            idx += 1

        if not sets:
            return

        args.append(run_id)
        query = f"UPDATE discovery.discovery_runs SET {', '.join(sets)} WHERE id = ${idx}"
        async with self.acquire() as conn:
            await conn.execute(query, *args)

    async def complete_discovery_run(
        self,
        run_id: UUID,
        *,
        summary: str | None = None,
        transcript: list | None = None,
        status: str = "completed",
    ) -> None:
        """Mark a run as complete and store summary + transcript."""
        async with self.acquire() as conn:
            await conn.execute(
                """
                UPDATE discovery.discovery_runs
                SET
                    completed_at = NOW(),
                    status = $2,
                    summary = $3,
                    agent_transcript = $4,
                    hosts_discovered = hosts_new + hosts_updated
                WHERE id = $1
                """,
                run_id,
                status,
                summary,
                json.dumps(transcript) if transcript is not None else None,
            )

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
        """
        UPSERT a host record.

        Supports IP-only, hostname-only, or IP+hostname records.
        Returns (host_id, is_new) where is_new=True on first insert.
        """
        if not ip_address and not hostname:
            raise ValueError("Either ip_address or hostname must be provided")

        meta_json = json.dumps(metadata or {})

        if ip_address:
            return await self._upsert_host_by_ip(
                network_id, ip_address, port,
                protocol=protocol, service_type=service_type, hostname=hostname,
                confidence=confidence, discovery_method=discovery_method,
                validator_id=validator_id, meta_json=meta_json,
            )
        return await self._upsert_host_by_hostname(
            network_id, hostname, port,  # type: ignore[arg-type]
            protocol=protocol, service_type=service_type,
            confidence=confidence, discovery_method=discovery_method,
            validator_id=validator_id, meta_json=meta_json,
        )

    async def _upsert_host_by_ip(
        self, network_id: UUID, ip_address: str, port: int | None, *,
        protocol: str | None, service_type: str | None, hostname: str | None,
        confidence: float, discovery_method: str | None,
        validator_id: UUID | None, meta_json: str,
    ) -> tuple[UUID, bool]:
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                WITH
                ip_existing AS (
                    SELECT id FROM discovery.hosts
                    WHERE network_id = $1
                      AND ip_address = $2::inet
                      AND (port IS NOT DISTINCT FROM $3)
                ),
                hostname_existing AS (
                    SELECT id FROM discovery.hosts
                    WHERE network_id = $1
                      AND hostname = $6
                      AND (port IS NOT DISTINCT FROM $3)
                      AND ip_address IS NULL
                      AND $6 IS NOT NULL
                ),
                upgraded AS (
                    UPDATE discovery.hosts
                    SET ip_address = $2::inet,
                        last_seen_at = NOW(),
                        confidence = GREATEST(confidence, $7),
                        last_discovery_method = COALESCE($8, last_discovery_method),
                        is_active = true
                    WHERE id IN (SELECT id FROM hostname_existing)
                      AND NOT EXISTS (SELECT 1 FROM ip_existing)
                    RETURNING id, false AS is_new
                ),
                inserted AS (
                    INSERT INTO discovery.hosts
                        (network_id, ip_address, port, protocol, service_type,
                         hostname, confidence, last_discovery_method,
                         validator_id, metadata, is_active, last_seen_at)
                    SELECT $1, $2::inet, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, true, NOW()
                    WHERE NOT EXISTS (SELECT 1 FROM ip_existing)
                      AND NOT EXISTS (SELECT 1 FROM hostname_existing)
                    RETURNING id, true AS is_new
                ),
                updated AS (
                    UPDATE discovery.hosts
                    SET
                        last_seen_at = NOW(),
                        confidence = GREATEST(confidence, $7),
                        hostname = COALESCE($6, hostname),
                        last_discovery_method = COALESCE($8, last_discovery_method),
                        is_active = true
                    WHERE network_id = $1
                      AND ip_address = $2::inet
                      AND (port IS NOT DISTINCT FROM $3)
                      AND NOT EXISTS (SELECT 1 FROM inserted)
                      AND NOT EXISTS (SELECT 1 FROM upgraded)
                    RETURNING id, false AS is_new
                )
                SELECT id, is_new FROM inserted
                UNION ALL
                SELECT id, is_new FROM upgraded
                UNION ALL
                SELECT id, is_new FROM updated
                LIMIT 1
                """,
                network_id, ip_address, port, protocol, service_type,
                hostname, confidence, discovery_method, validator_id, meta_json,
            )
        return row["id"], row["is_new"]

    async def _upsert_host_by_hostname(
        self, network_id: UUID, hostname: str, port: int | None, *,
        protocol: str | None, service_type: str | None,
        confidence: float, discovery_method: str | None,
        validator_id: UUID | None, meta_json: str,
    ) -> tuple[UUID, bool]:
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                WITH existing AS (
                    SELECT id FROM discovery.hosts
                    WHERE network_id = $1
                      AND hostname = $2
                      AND (port IS NOT DISTINCT FROM $3)
                ),
                inserted AS (
                    INSERT INTO discovery.hosts
                        (network_id, port, protocol, service_type,
                         hostname, confidence, last_discovery_method,
                         validator_id, metadata, is_active, last_seen_at)
                    SELECT $1, $3, $4, $5, $2, $6, $7, $8, $9::jsonb, true, NOW()
                    WHERE NOT EXISTS (SELECT 1 FROM existing)
                    RETURNING id, true AS is_new
                ),
                updated AS (
                    UPDATE discovery.hosts
                    SET
                        last_seen_at = NOW(),
                        confidence = GREATEST(confidence, $6),
                        last_discovery_method = COALESCE($7, last_discovery_method),
                        is_active = true
                    WHERE network_id = $1
                      AND hostname = $2
                      AND (port IS NOT DISTINCT FROM $3)
                      AND NOT EXISTS (SELECT 1 FROM inserted)
                    RETURNING id, false AS is_new
                )
                SELECT id, is_new FROM inserted
                UNION ALL
                SELECT id, is_new FROM updated
                LIMIT 1
                """,
                network_id, hostname, port, protocol, service_type,
                confidence, discovery_method, validator_id, meta_json,
            )
        return row["id"], row["is_new"]

    async def flag_host_gone(self, host_id: UUID, reason: str) -> bool:
        """Mark a host as inactive."""
        async with self.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE discovery.hosts
                SET
                    is_active = false,
                    metadata = jsonb_set(
                        COALESCE(metadata, '{}'::jsonb),
                        '{gone_reason}',
                        to_jsonb($2::text)
                    ),
                    last_seen_at = NOW()
                WHERE id = $1
                """,
                host_id,
                reason,
            )
        return result == "UPDATE 1"

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
        conditions = ["h.network_id = $1"]
        args: list[Any] = [network_id]
        idx = 2

        if operator_name is not None:
            conditions.append(f"v.operator_name = ${idx}")
            args.append(operator_name)
            idx += 1
        if service_type is not None:
            conditions.append(f"h.service_type = ${idx}")
            args.append(service_type)
            idx += 1
        if min_confidence is not None:
            conditions.append(f"h.confidence >= ${idx}")
            args.append(min_confidence)
            idx += 1
        if is_active is not None:
            conditions.append(f"h.is_active = ${idx}")
            args.append(is_active)
            idx += 1
        if not_seen_since is not None:
            conditions.append(f"h.last_seen_at < ${idx}")
            args.append(not_seen_since)
            idx += 1

        where = " AND ".join(conditions)
        query = f"""
            SELECT
                h.id, h.ip_address::text, h.port, h.protocol, h.service_type,
                h.hostname, h.confidence, h.last_seen_at, h.first_seen_at,
                h.last_discovery_method, h.is_active, h.metadata,
                v.pubkey AS validator_pubkey, v.operator_name
            FROM discovery.hosts h
            LEFT JOIN discovery.validators v ON h.validator_id = v.id
            WHERE {where}
            ORDER BY h.confidence DESC, h.last_seen_at DESC
            LIMIT ${idx}
        """
        args.append(limit)
        async with self.acquire() as conn:
            rows = await conn.fetch(query, *args)
        return [dict(r) for r in rows]

    async def get_validators(self, network_id: UUID) -> list[dict]:
        """Return all validators for a network with host counts."""
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    v.id, v.pubkey, v.operator_name, v.operator_domain,
                    COUNT(h.id) AS host_count,
                    COUNT(h.id) FILTER (WHERE h.is_active) AS active_host_count
                FROM discovery.validators v
                LEFT JOIN discovery.hosts h ON h.validator_id = v.id
                WHERE v.network_id = $1
                GROUP BY v.id, v.pubkey, v.operator_name, v.operator_domain
                ORDER BY active_host_count DESC
                """,
                network_id,
            )
        return [dict(r) for r in rows]

    async def get_or_create_validator(
        self, network_id: UUID, pubkey: str, operator_name: str | None = None
    ) -> UUID:
        """Get existing validator ID or insert a new one.

        operator_name is set on insert and preserved on conflict via COALESCE
        (existing name is not overwritten if the caller passes None).
        """
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO discovery.validators (network_id, pubkey, operator_name)
                VALUES ($1, $2, $3)
                ON CONFLICT (network_id, pubkey) DO UPDATE
                SET updated_at = NOW(),
                    operator_name = COALESCE(EXCLUDED.operator_name, discovery.validators.operator_name)
                RETURNING id
                """,
                network_id,
                pubkey,
                operator_name,
            )
        return row["id"]

    async def get_discovery_diff(
        self, network_id: UUID, since: datetime
    ) -> dict:
        """Return diff of hosts and validators since the given timestamp."""
        async with self.acquire() as conn:
            new_hosts = await conn.fetch(
                """
                SELECT id, ip_address::text, port, service_type, hostname, confidence
                FROM discovery.hosts
                WHERE network_id = $1 AND first_seen_at >= $2 AND is_active = true
                """,
                network_id,
                since,
            )
            gone_hosts = await conn.fetch(
                """
                SELECT id, ip_address::text, port, service_type
                FROM discovery.hosts
                WHERE network_id = $1 AND last_seen_at >= $2 AND is_active = false
                """,
                network_id,
                since,
            )
            changed_hosts = await conn.fetch(
                """
                SELECT id, ip_address::text, port, service_type, confidence, last_seen_at
                FROM discovery.hosts
                WHERE network_id = $1
                  AND last_seen_at >= $2
                  AND first_seen_at < $2
                  AND is_active = true
                """,
                network_id,
                since,
            )
            new_validators = await conn.fetch(
                "SELECT id, pubkey, operator_name FROM discovery.validators "
                "WHERE network_id = $1 AND created_at >= $2",
                network_id,
                since,
            )

        return {
            "new_hosts": [dict(r) for r in new_hosts],
            "gone_hosts": [dict(r) for r in gone_hosts],
            "changed_hosts": [dict(r) for r in changed_hosts],
            "new_validators": [dict(r) for r in new_validators],
            "since": since.isoformat(),
        }

    async def save_hypothesis(
        self,
        run_id: UUID,
        hypothesis: str,
        method: str,
        confidence_before: float,
        embedding: list[float] | None = None,
    ) -> UUID:
        """Save a discovery hypothesis record."""
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO discovery.discovery_hypotheses
                    (run_id, hypothesis, method, confidence_before, embedding)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id
                """,
                run_id,
                hypothesis,
                method,
                confidence_before,
                embedding,
            )
        return row["id"]

    async def search_hypotheses(
        self,
        embedding: list[float],
        min_success_rate: float | None = None,
        limit: int = 10,
    ) -> list[dict]:
        """Search past hypotheses by vector similarity.

        min_success_rate acts as a boolean filter: when provided, only
        validated=true rows are returned (no threshold arithmetic needed).
        All parameters use $N placeholders — no f-string interpolation.
        """
        conditions = ["embedding IS NOT NULL"]
        args: list = [embedding, limit]
        if min_success_rate is not None:
            conditions.append("validated = true")
        where = " AND ".join(conditions)
        query = f"""
            SELECT
                id, hypothesis, method, confidence_before,
                validated, hosts_found, created_at,
                1 - (embedding <=> $1::vector) AS similarity
            FROM discovery.discovery_hypotheses
            WHERE {where}
            ORDER BY embedding <=> $1::vector
            LIMIT $2
        """
        async with self.acquire() as conn:
            rows = await conn.fetch(query, *args)
        return [dict(r) for r in rows]

    async def get_recent_runs(self, network_id: UUID, limit: int = 5) -> list[dict]:
        """Return recent discovery runs for a network."""
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    id, started_at, completed_at, status,
                    hosts_discovered, hosts_new, hosts_updated, hosts_gone,
                    tool_calls, llm_tokens_used, summary
                FROM discovery.discovery_runs
                WHERE network_id = $1
                ORDER BY started_at DESC
                LIMIT $2
                """,
                network_id,
                limit,
            )
        return [dict(r) for r in rows]
