"""
Tests for NetworkRegistry: registration, schema combination, tool map.
"""

import pytest

from src.tools.registry import NetworkRegistry
from src.tools.blockchain.base import ChainTools
from src.tools.schemas import UNIVERSAL_TOOL_SCHEMAS


class MockChainTool:
    async def get_validators(self, **kwargs):
        return {"validators": [], "count": 0}

    async def get_committee(self, **kwargs):
        return {"committee": []}


class MockChainTools(ChainTools):
    def schemas(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "mock_get_validators",
                    "description": "Mock validator fetch",
                    "parameters": {"type": "object", "properties": {}, "required": []},
                },
            }
        ]

    def get_tool_map(self) -> dict:
        tool = MockChainTool()
        return {"mock_get_validators": tool.get_validators}

    def primary_tool_name(self) -> str:
        return "mock_get_validators"

    async def get_seed_hosts(self, network: str) -> list[dict]:
        return []


@pytest.fixture(autouse=True)
def reset_registry():
    """Clear registry before each test to avoid cross-test pollution."""
    NetworkRegistry.clear()
    yield
    NetworkRegistry.clear()


def test_register_and_retrieve():
    mock_tools = MockChainTools()
    NetworkRegistry.register("mock", mock_tools)
    assert "mock" in NetworkRegistry.registered_chains()


def test_get_chain_tools_raises_for_unknown():
    with pytest.raises(KeyError, match="No tools registered"):
        NetworkRegistry.get_chain_tools("unknown_chain")


def test_get_all_tool_schemas_combines_chain_and_universal():
    NetworkRegistry.register("mock", MockChainTools())
    schemas = NetworkRegistry.get_all_tool_schemas("mock")

    chain_names = {"mock_get_validators"}
    universal_names = {s["function"]["name"] for s in UNIVERSAL_TOOL_SCHEMAS}

    schema_names = {s["function"]["name"] for s in schemas}
    assert chain_names.issubset(schema_names)
    assert universal_names.issubset(schema_names)


def test_get_tool_map_includes_chain_tools():
    NetworkRegistry.register("mock", MockChainTools())
    tool_map = NetworkRegistry.get_tool_map("mock")
    assert "mock_get_validators" in tool_map
    assert callable(tool_map["mock_get_validators"])


def test_registered_chains_returns_list():
    NetworkRegistry.register("alpha", MockChainTools())
    NetworkRegistry.register("beta", MockChainTools())
    chains = NetworkRegistry.registered_chains()
    assert "alpha" in chains
    assert "beta" in chains


def test_register_overwrites_existing():
    first = MockChainTools()
    second = MockChainTools()
    NetworkRegistry.register("mock", first)
    NetworkRegistry.register("mock", second)
    assert NetworkRegistry.get_chain_tools("mock") is second


def test_register_universal_tools_and_retrieve():
    async def dns_fn(**kwargs):
        return {}

    NetworkRegistry.register_universal_tools({"dns_lookup": dns_fn})
    tool_map = NetworkRegistry.get_universal_tool_map()
    assert "dns_lookup" in tool_map
    assert tool_map["dns_lookup"] is dns_fn


def test_register_universal_tools_merges():
    async def fn_a(**kwargs):
        return {}

    async def fn_b(**kwargs):
        return {}

    NetworkRegistry.register_universal_tools({"tool_a": fn_a})
    NetworkRegistry.register_universal_tools({"tool_b": fn_b})
    tool_map = NetworkRegistry.get_universal_tool_map()
    assert "tool_a" in tool_map
    assert "tool_b" in tool_map


def test_clear_resets_universal_tools():
    async def fn(**kwargs):
        return {}

    NetworkRegistry.register_universal_tools({"some_tool": fn})
    NetworkRegistry.clear()
    assert NetworkRegistry.get_universal_tool_map() == {}
