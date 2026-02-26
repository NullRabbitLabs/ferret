"""
Configuration for Ferret.

All settings loaded from environment variables.
"""

import os
from dataclasses import dataclass


@dataclass
class Config:
    discovery_api_url: str
    llm_gateway_url: str
    llm_model: str
    rpc_urls: dict[str, str]
    max_tool_calls: int
    max_new_hosts: int
    max_idle_calls: int
    probe_rate_limit: int
    github_token: str | None
    serp_api_key: str | None
    host: str = "0.0.0.0"
    port: int = 8093
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Config":
        from src.networks import NETWORK_DEFINITIONS

        rpc_urls = {
            name: os.environ.get(env_var, default)
            for name, (_, env_var, default) in NETWORK_DEFINITIONS.items()
        }
        return cls(
            discovery_api_url=os.environ.get(
                "DISCOVERY_API_URL",
                "http://localhost:8092",
            ),
            llm_gateway_url=os.environ.get("LLM_GATEWAY_URL", "http://localhost:8090"),
            # deepseek-chat (V3) supports tool calls; deepseek-reasoner (R1) does not
            llm_model=os.environ.get("DISCOVERY_LLM_MODEL", "deepseek-chat"),
            rpc_urls=rpc_urls,
            max_tool_calls=int(os.environ.get("DISCOVERY_MAX_TOOL_CALLS", "30")),
            max_new_hosts=int(os.environ.get("DISCOVERY_MAX_NEW_HOSTS", "10")),
            max_idle_calls=int(os.environ.get("DISCOVERY_MAX_IDLE_CALLS", "15")),
            probe_rate_limit=int(
                os.environ.get("DISCOVERY_PROBE_RATE_LIMIT", "50")
            ),
            github_token=os.environ.get("GITHUB_TOKEN"),
            serp_api_key=os.environ.get("SERP_API_KEY"),
            host=os.environ.get("HOST", "0.0.0.0"),
            port=int(os.environ.get("PORT", "8093")),
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
        )
