"""
Configuration for the discovery agent service.

All settings loaded from environment variables.
"""

import os
from dataclasses import dataclass, field


@dataclass
class Config:
    database_url: str
    llm_gateway_url: str
    llm_model: str
    sui_rpc_url: str
    solana_rpc_url: str
    max_tool_calls: int
    max_new_hosts: int
    max_idle_calls: int
    probe_rate_limit: int
    github_token: str | None
    serp_api_key: str | None
    db_pool_min_size: int
    db_pool_max_size: int

    @classmethod
    def from_env(cls) -> "Config":
        database_url = os.environ.get(
            "DATABASE_URL",
            "postgresql://nr_scan:nr_scan_dev@localhost:5433/nr_scan",
        )
        return cls(
            database_url=database_url,
            llm_gateway_url=os.environ.get("LLM_GATEWAY_URL", "http://localhost:8090"),
            # deepseek-chat (V3) supports tool calls; deepseek-reasoner (R1) does not
            llm_model=os.environ.get("DISCOVERY_LLM_MODEL", "deepseek-chat"),
            sui_rpc_url=os.environ.get(
                "DISCOVERY_SUI_RPC", "https://fullnode.mainnet.sui.io:443"
            ),
            solana_rpc_url=os.environ.get(
                "DISCOVERY_SOLANA_RPC", "https://api.mainnet-beta.solana.com"
            ),
            max_tool_calls=int(os.environ.get("DISCOVERY_MAX_TOOL_CALLS", "30")),
            max_new_hosts=int(os.environ.get("DISCOVERY_MAX_NEW_HOSTS", "10")),
            max_idle_calls=int(os.environ.get("DISCOVERY_MAX_IDLE_CALLS", "15")),
            probe_rate_limit=int(
                os.environ.get("DISCOVERY_PROBE_RATE_LIMIT", "50")
            ),
            github_token=os.environ.get("GITHUB_TOKEN"),
            serp_api_key=os.environ.get("SERP_API_KEY"),
            db_pool_min_size=int(os.environ.get("DB_POOL_MIN_SIZE", "1")),
            db_pool_max_size=int(os.environ.get("DB_POOL_MAX_SIZE", "5")),
        )
