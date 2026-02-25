"""
Abstract base class for chain-specific tool sets.
"""

from abc import ABC, abstractmethod
from collections.abc import Callable


class ChainTools(ABC):
    """
    Abstract base for blockchain-specific tool implementations.

    Each chain (Sui, Solana, …) subclasses this and provides:
    - schemas(): list of OpenAI-format dicts for this chain's tools
    - get_tool_map(): dict mapping tool_name -> async callable
    """

    @abstractmethod
    def schemas(self) -> list[dict]:
        """Return OpenAI-format tool schemas for this chain's tools."""

    @abstractmethod
    def get_tool_map(self) -> dict[str, Callable]:
        """Return mapping of tool name -> async callable."""

    @abstractmethod
    def primary_tool_name(self) -> str:
        """Return the tool name to call first to get the validator/node set."""

    @abstractmethod
    async def get_seed_hosts(self, network: str) -> list[dict]:
        """Fetch on-chain data and return hosts ready for bulk_report_discovered_hosts."""
