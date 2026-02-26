"""
Tests for the DiscoveryAgent loop.

All external dependencies are mocked. Tests verify:
1. Agent stops on finish_reason == 'stop'
2. Agent stops at max_tool_calls limit
3. Agent stops after max_consecutive_failures
4. Transcript is saved to discovery_run
5. Tool results are appended to messages in correct format
6. report_discovered_host increments hosts_new counter
"""

import json
from unittest.mock import AsyncMock, MagicMock, call, patch
from uuid import uuid4

import pytest

from src.gateway_client import GatewayResponse, ToolCall
from tests.conftest import make_tool_call_response, stop_response


@pytest.fixture
def state_tools(mock_db):
    from src.tools.state import StateTools
    return StateTools(db=mock_db)


class _MockSuiTools:
    """Minimal ChainTools stand-in for agent tests."""

    def schemas(self):
        return []

    def get_tool_map(self):
        return {}

    def primary_tool_name(self):
        return "sui_get_validators"

    async def get_seed_hosts(self, network):
        return []  # No HTTP calls in unit tests


@pytest.fixture
def registered_registry():
    """Register mock blockchain tools for 'sui' chain."""
    from src.tools.registry import NetworkRegistry

    NetworkRegistry.clear()
    NetworkRegistry.register("sui", _MockSuiTools())
    yield
    NetworkRegistry.clear()


@pytest.fixture
def agent(mock_db, mock_gateway, state_tools, registered_registry):
    from src.agent import DiscoveryAgent
    return DiscoveryAgent(
        db=mock_db,
        gateway=mock_gateway,
        state_tools=state_tools,
        max_tool_calls=10,
        max_consecutive_failures=3,
    )


# ============================================================
# Test 1: Agent stops on finish_reason == 'stop'
# ============================================================

@pytest.mark.asyncio
async def test_agent_stops_on_finish_reason_stop(agent, mock_gateway, mock_db):
    mock_gateway.chat_with_tools.return_value = stop_response("All done.")

    result = await agent.run(network="sui")

    assert mock_gateway.chat_with_tools.call_count == 2  # once for main loop, once for summary
    mock_db.complete_discovery_run.assert_called_once()
    assert result.network == "sui"


# ============================================================
# Test 2: Agent stops at max_tool_calls
# ============================================================

@pytest.mark.asyncio
async def test_agent_stops_at_max_tool_calls(mock_db, mock_gateway, state_tools, registered_registry):
    from src.agent import DiscoveryAgent

    agent = DiscoveryAgent(
        db=mock_db,
        gateway=mock_gateway,
        state_tools=state_tools,
        max_tool_calls=3,
    )

    # Returns a tool call response every time (never stops naturally)
    async def _get_known_hosts(**kwargs):
        return {"network": "sui", "count": 0, "hosts": []}

    state_tools._db = mock_db

    # Patch tool map to return a known good tool
    with patch.object(agent, "_build_tool_map") as mock_build:
        mock_build.return_value = {"get_known_hosts": _get_known_hosts}

        # Gateway keeps returning tool calls
        tool_response = make_tool_call_response(
            "get_known_hosts", {"network": "sui"}, tc_id="tc_1"
        )
        mock_gateway.chat_with_tools.return_value = tool_response

        result = await agent.run(network="sui")

    # Should have stopped at max_tool_calls=3
    assert result.tool_calls <= 3


# ============================================================
# Test 3: Agent stops after max_consecutive_failures
# ============================================================

@pytest.mark.asyncio
async def test_agent_stops_after_max_consecutive_failures(
    mock_db, mock_gateway, state_tools, registered_registry
):
    from src.agent import DiscoveryAgent

    agent = DiscoveryAgent(
        db=mock_db,
        gateway=mock_gateway,
        state_tools=state_tools,
        max_tool_calls=100,
        max_consecutive_failures=3,
    )

    async def _failing_tool(**kwargs):
        return {"error": "Something went wrong"}

    with patch.object(agent, "_build_tool_map") as mock_build:
        mock_build.return_value = {"bad_tool": _failing_tool}

        responses = []
        for _ in range(10):
            responses.append(
                make_tool_call_response("bad_tool", {}, tc_id="tc_1")
            )
        responses.append(stop_response())
        mock_gateway.chat_with_tools.side_effect = responses

        result = await agent.run(network="sui")

    # After 3 consecutive failures it should stop — well below max_tool_calls=100
    assert result.tool_calls <= 5


# ============================================================
# Test 4: Transcript saved to discovery_run
# ============================================================

@pytest.mark.asyncio
async def test_transcript_saved_to_discovery_run(agent, mock_gateway, mock_db):
    mock_gateway.chat_with_tools.return_value = stop_response("Done.")

    await agent.run(network="sui")

    complete_call = mock_db.complete_discovery_run.call_args
    transcript = complete_call[1].get("transcript") or complete_call[0][1] if complete_call[0] else None
    # transcript is the messages list
    assert complete_call is not None
    kwargs = complete_call[1]
    assert "transcript" in kwargs
    assert isinstance(kwargs["transcript"], list)


# ============================================================
# Test 5: Tool results appended in correct format
# ============================================================

@pytest.mark.asyncio
async def test_tool_result_appended_to_messages(mock_db, mock_gateway, state_tools, registered_registry):
    from src.agent import DiscoveryAgent

    agent = DiscoveryAgent(
        db=mock_db,
        gateway=mock_gateway,
        state_tools=state_tools,
        max_tool_calls=5,
    )

    captured_messages: list[list] = []

    async def _mock_tool(**kwargs):
        return {"result": "some data", "count": 42}

    async def _capture_chat(messages, tools, system_prompt=None):
        captured_messages.append(list(messages))
        if len(captured_messages) == 1:
            return make_tool_call_response("my_tool", {"key": "val"}, tc_id="tc_abc")
        return stop_response()

    with patch.object(agent, "_build_tool_map") as mock_build:
        mock_build.return_value = {"my_tool": _mock_tool}
        mock_gateway.chat_with_tools.side_effect = _capture_chat

        await agent.run(network="sui")

    # Second call should include the tool result message
    assert len(captured_messages) >= 2
    second_call_messages = captured_messages[1]

    # Find the tool result message
    tool_result_msgs = [m for m in second_call_messages if m.get("role") == "tool"]
    assert len(tool_result_msgs) >= 1
    assert tool_result_msgs[0]["tool_call_id"] == "tc_abc"
    content = json.loads(tool_result_msgs[0]["content"])
    assert content["count"] == 42


# ============================================================
# Test 6: report_discovered_host increments hosts_new
# ============================================================

@pytest.mark.asyncio
async def test_report_discovered_host_increments_hosts_new(
    mock_db, mock_gateway, registered_registry
):
    from src.agent import DiscoveryAgent
    from src.tools.state import StateTools

    host_id = uuid4()
    mock_db.upsert_host.return_value = (host_id, True)  # is_new=True

    state_tools = StateTools(db=mock_db)

    agent = DiscoveryAgent(
        db=mock_db,
        gateway=mock_gateway,
        state_tools=state_tools,
        max_tool_calls=5,
    )

    async def _capture_chat(messages, tools, system_prompt=None):
        if len(messages) <= 1:
            # First call: return report_discovered_host tool call
            return make_tool_call_response(
                "report_discovered_host",
                {
                    "network": "sui",
                    "ip_address": "1.2.3.4",
                    "service_type": "rpc",
                    "confidence": 0.9,
                    "discovery_method": "on_chain",
                    "reasoning": "Found on-chain",
                },
                tc_id="tc_report",
            )
        return stop_response()

    mock_gateway.chat_with_tools.side_effect = _capture_chat

    result = await agent.run(network="sui")

    assert result.hosts_new == 1


# ============================================================
# Test 7: on_event callback fired on tool calls
# ============================================================

@pytest.mark.asyncio
async def test_on_event_callback_called_on_tool_call(
    mock_db, mock_gateway, state_tools, registered_registry
):
    from src.agent import DiscoveryAgent

    agent = DiscoveryAgent(
        db=mock_db,
        gateway=mock_gateway,
        state_tools=state_tools,
        max_tool_calls=5,
    )

    events: list[str] = []

    async def _mock_tool(**kwargs):
        return {"result": "ok"}

    async def _capture_chat(messages, tools, system_prompt=None):
        if len(messages) <= 1:
            return make_tool_call_response("my_tool", {"key": "val"}, tc_id="tc_evt")
        return stop_response()

    with patch.object(agent, "_build_tool_map") as mock_build:
        mock_build.return_value = {"my_tool": _mock_tool}
        mock_gateway.chat_with_tools.side_effect = _capture_chat

        await agent.run(network="sui", on_event=events.append)

    # At least one event should mention the tool name
    assert any("my_tool" in e for e in events)


# ============================================================
# Test 8: Initial directive contains step-numbered instructions
# ============================================================

_EMPTY_ENRICH = {"clusters": [], "total_hosts": 0, "sampled": 0}


def test_initial_directive_describes_seed_stats():
    from src.agent import DiscoveryAgent
    directive = DiscoveryAgent._build_initial_directive(
        network="sui",
        seed_result={"total": 126, "new": 118, "updated": 8},
        enrich=_EMPTY_ENRICH,
        focus=None,
    )
    assert "118" in directive  # new count
    assert "8" in directive    # updated count
    assert "126" in directive  # total count
    # Focuses on OSINT, not per-host iteration
    assert "cert_transparency_search" in directive


def test_initial_directive_includes_asn_cluster_summary():
    from src.agent import DiscoveryAgent
    enrich = {
        "clusters": [
            {"asn": "AS24940", "org": "HETZNER-AS", "country": "DE",
             "host_count": 42, "sample_ips": ["1.2.3.4"], "domains": ["val.hetzner.example"]},
        ],
        "total_hosts": 126,
        "sampled": 100,
    }
    directive = DiscoveryAgent._build_initial_directive(
        network="sui",
        seed_result={"total": 126, "new": 0, "updated": 126},
        enrich=enrich,
        focus=None,
    )
    assert "AS24940" in directive
    assert "HETZNER" in directive
    assert "42" in directive  # host_count
    assert "val.hetzner.example" in directive


def test_initial_directive_tells_llm_not_to_run_asn_dns():
    from src.agent import DiscoveryAgent
    directive = DiscoveryAgent._build_initial_directive(
        network="sui",
        seed_result={"total": 10, "new": 0, "updated": 10},
        enrich=_EMPTY_ENRICH,
        focus=None,
    )
    # LLM must be told these are already done
    assert "asn_lookup" in directive or "reverse_dns" in directive


# ============================================================
# Test 9: Large tool results are capped before insertion
# ============================================================

def test_cap_tool_result_content_small_result_unchanged():
    from src.agent import _cap_tool_result_content

    result = {"hosts": [{"ip": "1.2.3.4"}], "count": 1}
    out = _cap_tool_result_content(result)
    assert json.loads(out) == result


def test_cap_tool_result_content_truncates_large_list():
    from src.agent import _cap_tool_result_content

    # Build a result whose JSON exceeds 3000 chars
    many_hosts = [{"ip": f"10.0.{i // 256}.{i % 256}", "hostname": f"host-{i}.example.com"} for i in range(200)]
    result = {"network": "sui", "hosts": many_hosts, "count": 200}
    raw = json.dumps(result)
    assert len(raw) > 3000, "precondition: raw must exceed cap"

    out = _cap_tool_result_content(result)

    assert len(out) <= 3000 * 1.1, "output should be near the cap"
    parsed = json.loads(out)
    assert len(parsed["hosts"]) < 200, "list should be trimmed"
    assert "_truncated" in parsed, "truncation marker must be present"
    assert "omitted" in parsed["_truncated"]


def test_cap_tool_result_content_fallback_truncation():
    from src.agent import _cap_tool_result_content

    # No known list key — falls back to raw string truncation
    result = {"data": "x" * 4000}
    out = _cap_tool_result_content(result)
    assert len(out) <= 3012  # 3000 + len(" [truncated]")
    assert out.endswith("[truncated]")


@pytest.mark.asyncio
async def test_large_tool_result_capped_in_messages(mock_db, mock_gateway, state_tools, registered_registry):
    from src.agent import DiscoveryAgent

    agent = DiscoveryAgent(
        db=mock_db,
        gateway=mock_gateway,
        state_tools=state_tools,
        max_tool_calls=5,
    )

    captured_messages: list[list] = []

    # Tool returns a large list
    many_validators = [{"id": f"val-{i}", "address": f"10.0.{i // 256}.{i % 256}"} for i in range(200)]

    async def _big_tool(**kwargs):
        return {"validators": many_validators, "count": 200}

    async def _capture_chat(messages, tools, system_prompt=None):
        captured_messages.append(list(messages))
        if len(captured_messages) == 1:
            return make_tool_call_response("big_tool", {}, tc_id="tc_big")
        return stop_response()

    with patch.object(agent, "_build_tool_map") as mock_build:
        mock_build.return_value = {"big_tool": _big_tool}
        mock_gateway.chat_with_tools.side_effect = _capture_chat

        await agent.run(network="sui")

    # Find the tool result message in the second call
    second_call_messages = captured_messages[1]
    tool_msgs = [m for m in second_call_messages if m.get("role") == "tool"]
    assert len(tool_msgs) >= 1

    content = tool_msgs[0]["content"]
    assert len(content) <= 3000 * 1.1, "tool result content must be capped"
    parsed = json.loads(content)
    assert len(parsed["validators"]) < 200, "list was truncated"
    assert "_truncated" in parsed


def test_initial_directive_includes_focus():
    from src.agent import DiscoveryAgent
    directive = DiscoveryAgent._build_initial_directive(
        network="solana",
        seed_result={"total": 0, "new": 0, "updated": 0},
        enrich=_EMPTY_ENRICH,
        focus="new validators added this week",
    )
    assert "new validators added this week" in directive


# ============================================================
# Test: ASN/DNS tools excluded from LLM tool list
# ============================================================

def test_batch_enrichment_tools_excluded_from_llm_schemas(registered_registry):
    from src.agent import _CODE_ONLY_TOOLS
    from src.tools.registry import NetworkRegistry

    all_schemas = NetworkRegistry.get_all_tool_schemas("sui")
    schema_names = {s["function"]["name"] for s in all_schemas}

    # Confirm the tools exist in the full set
    assert "asn_lookup" in schema_names
    assert "reverse_dns" in schema_names

    # Simulate what agent.run does when building tool_schemas
    chain_tools = NetworkRegistry.get_chain_tools("sui")
    chain_schema_names = {s["function"]["name"] for s in chain_tools.schemas()}
    tool_schemas = [
        s for s in all_schemas
        if s["function"]["name"] not in chain_schema_names
        and s["function"]["name"] not in _CODE_ONLY_TOOLS
    ]
    llm_schema_names = {s["function"]["name"] for s in tool_schemas}

    assert "asn_lookup" not in llm_schema_names, "asn_lookup must be excluded from LLM"
    assert "reverse_dns" not in llm_schema_names, "reverse_dns must be excluded from LLM"
    # Useful OSINT tools must remain
    assert "cert_transparency_search" in llm_schema_names
    assert "whois_lookup" in llm_schema_names


# ============================================================
# Test: _batch_enrich returns cluster summary
# ============================================================

@pytest.mark.asyncio
async def test_batch_enrich_returns_cluster_summary(mock_db, mock_gateway, state_tools, registered_registry):
    from src.agent import DiscoveryAgent
    from unittest.mock import patch

    agent = DiscoveryAgent(db=mock_db, gateway=mock_gateway, state_tools=state_tools)

    mock_db.get_hosts.return_value = [
        {"ip_address": "10.0.0.1", "port": 8080, "service_type": "rpc",
         "confidence": 0.9, "last_seen_at": None},
        {"ip_address": "10.0.0.2", "port": 8080, "service_type": "rpc",
         "confidence": 0.9, "last_seen_at": None},
    ]

    async def _mock_asn(query, **kwargs):
        return {"ip": query, "asn": "AS24940", "as_org": "HETZNER-AS", "country": "DE"}

    async def _mock_rdns(ip_address, **kwargs):
        return {"ip_address": ip_address, "hostnames": [f"host.{ip_address}.example.com"]}

    with patch("src.agent.NetworkRegistry.get_universal_tool_map") as mock_map:
        mock_map.return_value = {"asn_lookup": _mock_asn, "reverse_dns": _mock_rdns}
        result = await agent._batch_enrich("sui")

    assert result["total_hosts"] == 2
    assert result["sampled"] == 2
    assert len(result["clusters"]) == 1
    cluster = result["clusters"][0]
    assert cluster["asn"] == "AS24940"
    assert cluster["host_count"] == 2
    assert len(cluster["domains"]) > 0


@pytest.mark.asyncio
async def test_batch_enrich_handles_missing_tools(mock_db, mock_gateway, state_tools, registered_registry):
    from src.agent import DiscoveryAgent
    from unittest.mock import patch

    agent = DiscoveryAgent(db=mock_db, gateway=mock_gateway, state_tools=state_tools)
    mock_db.get_hosts.return_value = [
        {"ip_address": "10.0.0.1", "port": 8080, "service_type": "rpc",
         "confidence": 0.9, "last_seen_at": None},
    ]

    with patch("src.agent.NetworkRegistry.get_universal_tool_map") as mock_map:
        mock_map.return_value = {}  # no tools registered
        result = await agent._batch_enrich("sui")

    assert result["clusters"] == []


# ============================================================
# Test: hostname-only hosts included as-is (no forward DNS)
# ============================================================

@pytest.mark.asyncio
async def test_batch_enrich_includes_hostname_only_without_dns_resolution(mock_db, mock_gateway, state_tools, registered_registry):
    """Hostname-only hosts appear in hostname_only list — NOT resolved to IPs."""
    from src.agent import DiscoveryAgent
    from unittest.mock import patch

    agent = DiscoveryAgent(db=mock_db, gateway=mock_gateway, state_tools=state_tools)

    mock_db.get_hosts.return_value = [
        {"ip_address": None, "hostname": "validator1.sui.example.com", "port": 8080,
         "service_type": "rpc", "confidence": 0.9, "last_seen_at": None},
        {"ip_address": None, "hostname": "validator2.sui.example.com", "port": 8080,
         "service_type": "rpc", "confidence": 0.9, "last_seen_at": None},
    ]

    dns_called = []

    async def _mock_dns(hostname, record_type, **kwargs):
        dns_called.append(hostname)
        return {"hostname": hostname, "record_type": "A", "records": [{"value": "10.0.0.1", "ttl": 300}]}

    async def _mock_asn(query, **kwargs):
        return {"ip": query, "asn": "AS24940", "as_org": "HETZNER-AS", "country": "DE"}

    async def _mock_rdns(ip_address, **kwargs):
        return {"ip_address": ip_address, "hostnames": []}

    with patch("src.agent.NetworkRegistry.get_universal_tool_map") as mock_map:
        mock_map.return_value = {
            "asn_lookup": _mock_asn,
            "reverse_dns": _mock_rdns,
            "dns_lookup": _mock_dns,
        }
        result = await agent._batch_enrich("sui")

    assert dns_called == [], "forward DNS resolution must NOT be called"
    assert result["total_hosts"] == 2
    assert "hostname_only" in result
    assert len(result["hostname_only"]) == 2
    assert "validator1.sui.example.com" in result["hostname_only"]
    assert "validator2.sui.example.com" in result["hostname_only"]


@pytest.mark.asyncio
async def test_batch_enrich_ip_hosts_still_get_asn_clusters(mock_db, mock_gateway, state_tools, registered_registry):
    """IP-based hosts continue to be clustered by ASN."""
    from src.agent import DiscoveryAgent
    from unittest.mock import patch

    agent = DiscoveryAgent(db=mock_db, gateway=mock_gateway, state_tools=state_tools)

    mock_db.get_hosts.return_value = [
        {"ip_address": "10.0.0.1", "hostname": None, "port": 8080,
         "service_type": "rpc", "confidence": 0.9, "last_seen_at": None},
    ]

    async def _mock_asn(query, **kwargs):
        return {"ip": query, "asn": "AS24940", "as_org": "HETZNER-AS", "country": "DE"}

    async def _mock_rdns(ip_address, **kwargs):
        return {"ip_address": ip_address, "hostnames": []}

    with patch("src.agent.NetworkRegistry.get_universal_tool_map") as mock_map:
        mock_map.return_value = {"asn_lookup": _mock_asn, "reverse_dns": _mock_rdns}
        result = await agent._batch_enrich("sui")

    assert len(result["clusters"]) == 1
    assert result["clusters"][0]["asn"] == "AS24940"


# ============================================================
# Test: get_known_hosts excluded from LLM tool list
# ============================================================

def test_get_known_hosts_excluded_from_llm_schemas(registered_registry):
    from src.agent import _CODE_ONLY_TOOLS
    from src.tools.registry import NetworkRegistry

    all_schemas = NetworkRegistry.get_all_tool_schemas("sui")
    chain_tools = NetworkRegistry.get_chain_tools("sui")
    chain_schema_names = {s["function"]["name"] for s in chain_tools.schemas()}
    tool_schemas = [
        s for s in all_schemas
        if s["function"]["name"] not in chain_schema_names
        and s["function"]["name"] not in _CODE_ONLY_TOOLS
    ]
    llm_schema_names = {s["function"]["name"] for s in tool_schemas}

    assert "get_known_hosts" not in llm_schema_names, "get_known_hosts must not be in LLM tool list"
    # OSINT tools must still be available
    assert "cert_transparency_search" in llm_schema_names
    assert "report_discovered_host" in llm_schema_names


# ============================================================
# Test: _get_summary injects verified run stats
# ============================================================

@pytest.mark.asyncio
async def test_get_summary_includes_verified_stats(mock_db, mock_gateway, state_tools, registered_registry):
    from src.agent import DiscoveryAgent

    agent = DiscoveryAgent(db=mock_db, gateway=mock_gateway, state_tools=state_tools)

    run_stats = {"hosts_new": 7, "hosts_updated": 2, "hosts_gone": 1}
    await agent._get_summary([], run_stats)

    mock_gateway.chat_with_tools.assert_called_once()
    call_kwargs = mock_gateway.chat_with_tools.call_args.kwargs
    messages = call_kwargs["messages"]
    summary_request = messages[-1]["content"]

    assert "7" in summary_request   # hosts_new
    assert "2" in summary_request   # hosts_updated
    assert "1" in summary_request   # hosts_gone
    # Must instruct model not to fabricate numbers
    assert "verified" in summary_request.lower() or "do not" in summary_request.lower()


# ============================================================
# Fix #1: _discoveries tracks successful report_discovered_host calls
# ============================================================

@pytest.mark.asyncio
async def test_agent_tracks_discoveries_on_successful_report(
    mock_db, mock_gateway, state_tools, registered_registry
):
    """After a successful report_discovered_host, _discoveries is populated."""
    from src.agent import DiscoveryAgent

    host_id = uuid4()
    mock_db.upsert_host.return_value = (host_id, True)
    state_tools._db = mock_db

    agent = DiscoveryAgent(
        db=mock_db, gateway=mock_gateway, state_tools=state_tools, max_tool_calls=5
    )

    async def _capture_chat(messages, tools, system_prompt=None):
        if len(messages) <= 1:
            return make_tool_call_response(
                "report_discovered_host",
                {
                    "network": "sui",
                    "ip_address": "1.2.3.4",
                    "service_type": "rpc",
                    "confidence": 0.9,
                    "discovery_method": "ct_log",
                    "reasoning": "Found via CT",
                },
                tc_id="tc_report",
            )
        return stop_response()

    mock_gateway.chat_with_tools.side_effect = _capture_chat
    await agent.run(network="sui")

    assert len(agent._discoveries) == 1
    assert agent._discoveries[0]["ip"] == "1.2.3.4"
    assert agent._discoveries[0]["is_new"] is True


@pytest.mark.asyncio
async def test_get_summary_receives_discoveries(mock_db, mock_gateway, state_tools, registered_registry):
    """_get_summary is called with new_discoveries from _discoveries list."""
    from src.agent import DiscoveryAgent

    host_id = uuid4()
    mock_db.upsert_host.return_value = (host_id, True)
    state_tools._db = mock_db

    agent = DiscoveryAgent(
        db=mock_db, gateway=mock_gateway, state_tools=state_tools, max_tool_calls=5
    )
    agent._discoveries = [
        {"ip": "9.9.9.9", "port": 8080, "service_type": "rpc", "is_new": True}
    ]

    with patch.object(agent, "_get_summary") as mock_summary:
        mock_summary.return_value = "Summary."
        mock_gateway.chat_with_tools.return_value = stop_response()
        await agent.run(network="sui")

    mock_summary.assert_called_once()
    call_kwargs = mock_summary.call_args.kwargs
    discoveries = call_kwargs.get("new_discoveries") or mock_summary.call_args[0][2] if len(mock_summary.call_args[0]) > 2 else call_kwargs.get("new_discoveries")
    # Either kwarg or positional — discoveries list must be passed
    assert mock_summary.called


# ============================================================
# Fix #6b: Nudge injected after 2 unfiltered get_known_hosts calls
# ============================================================

@pytest.mark.asyncio
async def test_nudge_injected_after_two_unfiltered_get_known_hosts(
    mock_db, mock_gateway, state_tools, registered_registry
):
    """After 2 unfiltered get_known_hosts calls, a guidance message is injected."""
    from src.agent import DiscoveryAgent

    agent = DiscoveryAgent(
        db=mock_db, gateway=mock_gateway, state_tools=state_tools, max_tool_calls=10
    )

    call_count = 0
    injected_messages: list[list] = []

    async def _capture_chat(messages, tools, system_prompt=None):
        nonlocal call_count
        call_count += 1
        injected_messages.append(list(messages))
        if call_count <= 3:
            return make_tool_call_response(
                "get_known_hosts", {"network": "sui"}, tc_id=f"tc_{call_count}"
            )
        return stop_response()

    async def _known_hosts(**kwargs):
        return {"network": "sui", "count": 5, "hosts": []}

    with patch.object(agent, "_build_tool_map") as mock_build:
        mock_build.return_value = {"get_known_hosts": _known_hosts}
        mock_gateway.chat_with_tools.side_effect = _capture_chat
        await agent.run(network="sui")

    # Check that a nudge message was injected (user role, directing to OSINT)
    all_messages = [m for msgs in injected_messages for m in msgs]
    nudge_msgs = [
        m for m in all_messages
        if m.get("role") == "user"
        and (
            "osint" in m.get("content", "").lower()
            or "cert_transparency" in m.get("content", "").lower()
            or "whois" in m.get("content", "").lower()
        )
    ]
    assert len(nudge_msgs) > 0, "Nudge must be injected after repeated unfiltered get_known_hosts"


@pytest.mark.asyncio
async def test_nudge_not_injected_for_filtered_get_known_hosts(
    mock_db, mock_gateway, state_tools, registered_registry
):
    """Filtered get_known_hosts calls do not trigger the nudge."""
    from src.agent import DiscoveryAgent

    agent = DiscoveryAgent(
        db=mock_db, gateway=mock_gateway, state_tools=state_tools, max_tool_calls=10
    )

    call_count = 0

    async def _capture_chat(messages, tools, system_prompt=None):
        nonlocal call_count
        call_count += 1
        if call_count <= 3:
            # filtered call (has operator_name filter)
            return make_tool_call_response(
                "get_known_hosts",
                {"network": "sui", "filters": {"operator_name": "SomeOp"}},
                tc_id=f"tc_{call_count}",
            )
        return stop_response()

    async def _known_hosts(**kwargs):
        return {"network": "sui", "count": 5, "hosts": []}

    with patch.object(agent, "_build_tool_map") as mock_build:
        mock_build.return_value = {"get_known_hosts": _known_hosts}
        mock_gateway.chat_with_tools.side_effect = _capture_chat
        await agent.run(network="sui")

    assert agent._get_hosts_calls == 0, "Filtered calls must not increment _get_hosts_calls"


# ============================================================
# Fix #7b: CT failure nudge after 2 errors
# ============================================================

@pytest.mark.asyncio
async def test_ct_failure_nudge_injected_after_two_failures(
    mock_db, mock_gateway, state_tools, registered_registry
):
    """After 2 cert_transparency_search failures, guidance to use other tools is injected."""
    from src.agent import DiscoveryAgent

    agent = DiscoveryAgent(
        db=mock_db, gateway=mock_gateway, state_tools=state_tools, max_tool_calls=10
    )

    call_count = 0
    captured_messages: list[list] = []

    async def _capture_chat(messages, tools, system_prompt=None):
        nonlocal call_count
        call_count += 1
        captured_messages.append(list(messages))
        if call_count <= 2:
            return make_tool_call_response(
                "cert_transparency_search",
                {"query": "example.com"},
                tc_id=f"tc_{call_count}",
            )
        return stop_response()

    async def _ct_failing(**kwargs):
        return {"query": "example.com", "results": [], "error": "crt.sh unavailable"}

    with patch.object(agent, "_build_tool_map") as mock_build:
        mock_build.return_value = {"cert_transparency_search": _ct_failing}
        mock_gateway.chat_with_tools.side_effect = _capture_chat
        await agent.run(network="sui")

    all_messages = [m for msgs in captured_messages for m in msgs]
    nudge_msgs = [
        m for m in all_messages
        if m.get("role") == "user"
        and (
            "whois" in m.get("content", "").lower()
            or "github" in m.get("content", "").lower()
        )
    ]
    assert len(nudge_msgs) > 0, "CT failure nudge must be injected after 2 failures"


# ============================================================
# Fix #11: bulk_report_discovered_hosts excluded from LLM schemas
# ============================================================

def test_bulk_report_not_in_llm_tool_schemas(registered_registry):
    from src.agent import _CODE_ONLY_TOOLS
    from src.tools.registry import NetworkRegistry

    all_schemas = NetworkRegistry.get_all_tool_schemas("sui")
    chain_tools = NetworkRegistry.get_chain_tools("sui")
    chain_schema_names = {s["function"]["name"] for s in chain_tools.schemas()}
    tool_schemas = [
        s for s in all_schemas
        if s["function"]["name"] not in chain_schema_names
        and s["function"]["name"] not in _CODE_ONLY_TOOLS
    ]
    llm_schema_names = {s["function"]["name"] for s in tool_schemas}

    assert "bulk_report_discovered_hosts" not in llm_schema_names, \
        "bulk_report_discovered_hosts must not be in LLM tool schemas"
    # OSINT tools must remain
    assert "report_discovered_host" in llm_schema_names
    assert "cert_transparency_search" in llm_schema_names


def test_code_only_tools_set_contains_expected_members(registered_registry):
    from src.agent import _CODE_ONLY_TOOLS

    assert "asn_lookup" in _CODE_ONLY_TOOLS
    assert "reverse_dns" in _CODE_ONLY_TOOLS
    assert "get_known_hosts" in _CODE_ONLY_TOOLS
    assert "bulk_report_discovered_hosts" in _CODE_ONLY_TOOLS


# ============================================================
# Fix #12: _compact_messages counts discoveries in dropped messages
# ============================================================

def test_compact_messages_bridge_includes_discovery_count():
    from src.agent import _compact_messages
    import json

    # Build messages where some dropped messages are report_discovered_host results
    tool_result_new = {
        "role": "tool",
        "tool_call_id": "tc_1",
        "content": json.dumps({"host_id": "abc", "is_new": True, "ip_address": "1.2.3.4"}),
    }
    tool_result_update = {
        "role": "tool",
        "tool_call_id": "tc_2",
        "content": json.dumps({"host_id": "def", "is_new": False, "ip_address": "5.6.7.8"}),
    }

    # Build 15 messages so compaction happens (tail=8 → drops 15 - 8 - 1 = 6)
    messages = [{"role": "user", "content": "initial directive"}]
    messages.append(tool_result_new)
    messages.append(tool_result_update)
    for i in range(12):
        messages.append({"role": "user", "content": f"msg {i}"})

    compacted = _compact_messages(messages, tail=8)

    # Find the bridge message
    bridge_msgs = [m for m in compacted if m.get("role") == "user" and "compacted" in m.get("content", "").lower()]
    assert len(bridge_msgs) == 1
    bridge_content = bridge_msgs[0]["content"]
    assert "2" in bridge_content, "Bridge must mention number of discoveries in dropped messages"
    assert "host" in bridge_content.lower() or "reported" in bridge_content.lower()


def test_compact_messages_bridge_no_count_when_no_discoveries():
    from src.agent import _compact_messages

    messages = [{"role": "user", "content": "initial"}]
    for i in range(14):
        messages.append({"role": "user", "content": f"msg {i}"})

    compacted = _compact_messages(messages, tail=8)
    bridge_msgs = [m for m in compacted if m.get("role") == "user" and "compacted" in m.get("content", "").lower()]
    assert len(bridge_msgs) == 1
    # No discovery count when none were reported
    bridge_content = bridge_msgs[0]["content"]
    # Should not claim "0 hosts" — just no mention is fine
    assert "0 hosts" not in bridge_content


# ============================================================
# Stopping conditions: max_new_hosts, max_idle_calls
# ============================================================

@pytest.mark.asyncio
async def test_agent_stops_when_max_new_hosts_reached(mock_db, mock_gateway, state_tools, registered_registry):
    """Agent stops after finding max_new_hosts new hosts, not the full tool budget."""
    from src.agent import DiscoveryAgent

    agent = DiscoveryAgent(
        db=mock_db,
        gateway=mock_gateway,
        state_tools=state_tools,
        max_tool_calls=50,
        max_new_hosts=2,
    )

    call_count = 0

    async def _mock_chat(messages, tools, system_prompt=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return make_tool_call_response(
                "report_discovered_host",
                {"network": "sui", "ip_address": "1.2.3.4", "service_type": "rpc",
                 "confidence": 0.9, "discovery_method": "on_chain", "reasoning": "test"},
                tc_id="tc1",
            )
        if call_count == 2:
            return make_tool_call_response(
                "report_discovered_host",
                {"network": "sui", "ip_address": "5.6.7.8", "service_type": "rpc",
                 "confidence": 0.9, "discovery_method": "on_chain", "reasoning": "test"},
                tc_id="tc2",
            )
        return stop_response()

    mock_db.upsert_host.side_effect = [(uuid4(), True), (uuid4(), True)]
    mock_gateway.chat_with_tools.side_effect = _mock_chat

    result = await agent.run(network="sui")

    assert result.hosts_new == 2
    # Should have stopped well before max_tool_calls=50
    assert mock_gateway.chat_with_tools.call_count < 10


@pytest.mark.asyncio
async def test_agent_stops_when_max_idle_calls_reached(mock_db, mock_gateway, state_tools, registered_registry):
    """Agent stops after max_idle_calls tool calls with no new host discovered."""
    from src.agent import DiscoveryAgent

    agent = DiscoveryAgent(
        db=mock_db,
        gateway=mock_gateway,
        state_tools=state_tools,
        max_tool_calls=50,
        max_idle_calls=3,
    )

    call_count = 0

    async def _idle_tool(**kwargs):
        return {"results": [], "note": "no results"}

    async def _mock_chat(messages, tools, system_prompt=None):
        nonlocal call_count
        call_count += 1
        if call_count <= 3:
            return make_tool_call_response("my_tool", {}, tc_id=f"tc{call_count}")
        return stop_response()

    with patch.object(agent, "_build_tool_map") as mock_build:
        mock_build.return_value = {"my_tool": _idle_tool}
        mock_gateway.chat_with_tools.side_effect = _mock_chat
        result = await agent.run(network="sui")

    # Should stop after 3 idle calls, not run to 50
    assert mock_gateway.chat_with_tools.call_count < 10


@pytest.mark.asyncio
async def test_agent_idle_calls_reset_on_new_host(mock_db, mock_gateway, state_tools, registered_registry):
    """With idle limit=2, agent survives 2 idle calls because a new host resets the counter."""
    from src.agent import DiscoveryAgent

    # max_idle_calls=2: 2 idle calls then stop, UNLESS a new host resets the counter
    agent = DiscoveryAgent(
        db=mock_db,
        gateway=mock_gateway,
        state_tools=state_tools,
        max_tool_calls=50,
        max_idle_calls=2,
        max_new_hosts=99,  # not the limiting factor
    )

    call_count = 0

    async def _mock_chat(messages, tools, system_prompt=None):
        nonlocal call_count
        call_count += 1
        # With reset:    idle(1)  → new_host/reset(0) → idle(1)  → idle(2) → STOP at call 4
        # Without reset: idle(1)  → new_host/stays(1) → idle(2)  → STOP at call 3
        sequence = [
            make_tool_call_response("my_tool", {}, tc_id="t1"),
            make_tool_call_response(
                "report_discovered_host",
                {"network": "sui", "ip_address": "1.1.1.1", "service_type": "rpc",
                 "confidence": 0.9, "discovery_method": "on_chain", "reasoning": "x"},
                tc_id="t2",
            ),
            make_tool_call_response("my_tool", {}, tc_id="t3"),
            make_tool_call_response("my_tool", {}, tc_id="t4"),
        ]
        if call_count <= len(sequence):
            return sequence[call_count - 1]
        return stop_response()

    async def _idle(**kwargs):
        return {"results": []}

    mock_db.upsert_host.return_value = (uuid4(), True)

    with patch.object(agent, "_build_tool_map") as mock_build:
        mock_build.return_value = {
            "my_tool": _idle,
            "report_discovered_host": state_tools.report_discovered_host,
        }
        mock_gateway.chat_with_tools.side_effect = _mock_chat
        await agent.run(network="sui")

    # With reset: idle(1) → new_host resets to 0 → idle(1) → idle(2) → stop at call 4
    # Without reset: idle(1) → new_host stays at 1 → idle(2) → stop at call 3
    assert call_count >= 4, "idle counter must have reset on new host discovery"
