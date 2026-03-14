"""
Network registry — single source of truth for all supported chains.

Pure data lives in networks.json (schema in networks.schema.json).
To add a new network:
  1. Create src/tools/blockchain/<name>.py with a ChainTools subclass
  2. Add an entry to networks.json

No code changes required here — ChainTools subclasses self-register via
__init_subclass__, and blockchain/__init__.py auto-imports all modules.
"""

import json
from pathlib import Path

import src.tools.blockchain  # noqa: F401 — triggers auto-registration of all ChainTools subclasses
from pydantic import BaseModel, HttpUrl, field_validator
from src.tools.blockchain.base import ChainTools


class NetworkConfig(BaseModel):
    env_var: str
    default_rpc_url: str
    allowed_ports: list[int]
    description: str = ""
    chain_type: str | None = None

    @field_validator("default_rpc_url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        HttpUrl(v)  # side-effect validation; return plain str to avoid Pydantic v2 URL object
        return v

    @field_validator("env_var")
    @classmethod
    def validate_env_var(cls, v: str) -> str:
        if not v or v != v.upper():
            raise ValueError("env_var must be non-empty UPPER_SNAKE_CASE")
        return v

    @field_validator("allowed_ports")
    @classmethod
    def validate_ports(cls, v: list[int]) -> list[int]:
        if not v:
            raise ValueError("allowed_ports must be non-empty")
        for port in v:
            if not 1 <= port <= 65535:
                raise ValueError(f"port {port} out of range 1–65535")
        return v


def _load_networks() -> dict[str, NetworkConfig]:
    path = Path(__file__).parent / "networks.json"
    data = json.loads(path.read_text())
    return {
        name: NetworkConfig.model_validate(entry)
        for name, entry in data.items()
        if name != "$schema"
    }


def _build_network_definitions(
    configs: dict[str, NetworkConfig],
    class_registry: dict[str, type] | None = None,
) -> dict[str, tuple]:
    if class_registry is None:
        class_registry = ChainTools._registry
    result = {}
    for name, cfg in configs.items():
        ct = cfg.chain_type or name
        if ct not in class_registry:
            raise KeyError(
                f"Network {name!r} (chain_type={ct!r}) has no ChainTools subclass. "
                f"Create src/tools/blockchain/{ct}.py with a ChainTools subclass."
            )
        result[name] = (class_registry[ct], cfg.env_var, cfg.default_rpc_url)
    return result


_NETWORK_CONFIGS: dict[str, NetworkConfig] = _load_networks()

# (tools_class, env_var, default_rpc_url) — same shape as before; cli.py, server.py, config.py unchanged
NETWORK_DEFINITIONS: dict[str, tuple] = _build_network_definitions(_NETWORK_CONFIGS)

# Union of all networks' allowed ports — consumed by src/tools/network.py
ALL_ALLOWED_PORTS: set[int] = {
    port for cfg in _NETWORK_CONFIGS.values() for port in cfg.allowed_ports
}
