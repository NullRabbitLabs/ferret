"""
Discovery run data classes.

These dataclasses represent discovery run state and are shared between
the API client and the agent.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass
class DiscoveryRun:
    id: UUID
    network_name: str
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
