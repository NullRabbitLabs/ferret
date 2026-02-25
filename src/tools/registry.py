"""
NetworkRegistry: maps chain_type -> ChainTools implementations.

Usage:
    from src.tools.registry import NetworkRegistry
    NetworkRegistry.register("sui", SuiTools(rpc_url=...))
    all_schemas = NetworkRegistry.get_all_tool_schemas("sui")
    tool_map = NetworkRegistry.get_tool_map("sui")
"""

from src.tools.blockchain.base import ChainTools
from src.tools.schemas import UNIVERSAL_TOOL_SCHEMAS


class NetworkRegistry:
    """Maps chain_type strings to ChainTools implementations."""

    _chains: dict[str, ChainTools] = {}
    _universal_tools: dict = {}

    @classmethod
    def register(cls, chain_type: str, tools: ChainTools) -> None:
        """Register a ChainTools implementation for a chain type."""
        cls._chains[chain_type] = tools

    @classmethod
    def register_universal_tools(cls, tool_map: dict) -> None:
        """Register (or update) the universal tool callables (dns, network, osint)."""
        cls._universal_tools.update(tool_map)

    @classmethod
    def get_universal_tool_map(cls) -> dict:
        """Return a copy of the universal tool map."""
        return dict(cls._universal_tools)

    @classmethod
    def get_chain_tools(cls, chain_type: str) -> ChainTools:
        """Return the ChainTools for a chain type."""
        if chain_type not in cls._chains:
            raise KeyError(f"No tools registered for chain_type '{chain_type}'")
        return cls._chains[chain_type]

    @classmethod
    def get_tool_map(cls, chain_type: str) -> dict:
        """Return combined tool_name -> callable map (chain + universal tools)."""
        return cls._chains[chain_type].get_tool_map()

    @classmethod
    def get_all_tool_schemas(cls, chain_type: str) -> list[dict]:
        """Return all OpenAI-format schemas: chain-specific + universal."""
        chain_tools = cls.get_chain_tools(chain_type)
        return chain_tools.schemas() + UNIVERSAL_TOOL_SCHEMAS

    @classmethod
    def registered_chains(cls) -> list[str]:
        """Return list of registered chain type names."""
        return list(cls._chains.keys())

    @classmethod
    def clear(cls) -> None:
        """Clear all registered chains and universal tools. Used in tests."""
        cls._chains = {}
        cls._universal_tools = {}
