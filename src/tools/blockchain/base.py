"""
Abstract base class for chain-specific tool sets.

ChainTools subclasses self-register in ChainTools._registry when their module
is imported. The key is the last component of the module name (e.g.
src.tools.blockchain.cosmos → "cosmos"), which must match the network name
in networks.json.
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

    # Populated automatically by __init_subclass__ as chain modules are imported.
    _registry: dict[str, type] = {}

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        # Register by the last dotted component of the defining module's name,
        # e.g. "src.tools.blockchain.cosmos" → "cosmos".
        network_name = cls.__module__.rsplit(".", 1)[-1]
        ChainTools._registry[network_name] = cls

    @abstractmethod
    def schemas(self) -> list[dict]:
        """Return OpenAI-format tool schemas for this chain's tools."""

    @abstractmethod
    def get_tool_map(self) -> dict[str, Callable]:
        """Return mapping of tool name -> async callable."""

    @abstractmethod
    def primary_tool_name(self) -> str:
        """Return the tool name to call first to get the validator/node set."""

    def seeding_only_tools(self) -> set[str]:
        """Tool names used only in Phase 1 code seeding — excluded from LLM tool list.

        Default: all tools in schemas(). Override to keep some tools available
        to the LLM (e.g. sui_enumerate_peers).
        """
        return {s["function"]["name"] for s in self.schemas()}

    @abstractmethod
    async def get_seed_hosts(self, network: str) -> list[dict]:
        """Fetch on-chain data and return hosts ready for bulk_report_discovered_hosts."""
