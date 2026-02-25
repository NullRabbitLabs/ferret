"""
Tests for honest summary generation in DiscoveryAgent._get_summary (Fix #1).

Summary must use structured discovery data, not fabricate host names.
"""

from unittest.mock import AsyncMock

import pytest

from src.gateway_client import GatewayResponse


@pytest.fixture
def state_tools_mock():
    from src.tools.state import StateTools
    return AsyncMock(spec=StateTools)


@pytest.fixture
def agent(mock_db, mock_gateway, state_tools_mock):
    from src.agent import DiscoveryAgent
    return DiscoveryAgent(db=mock_db, gateway=mock_gateway, state_tools=state_tools_mock)


@pytest.mark.asyncio
async def test_summary_includes_structured_discoveries(agent, mock_gateway):
    """When new_discoveries list is passed, IPs appear in the summary prompt."""
    mock_gateway.chat_with_tools.return_value = GatewayResponse(
        finish_reason="stop", tool_calls=[], text="Summary here."
    )
    discoveries = [
        {"ip": "1.2.3.4", "port": 8080, "service_type": "rpc", "is_new": True},
        {"ip": "5.6.7.8", "port": 8084, "service_type": "p2p", "is_new": False},
    ]
    run_stats = {"hosts_new": 1, "hosts_updated": 1, "hosts_gone": 0}
    await agent._get_summary([], run_stats, new_discoveries=discoveries)

    call_kwargs = mock_gateway.chat_with_tools.call_args.kwargs
    messages = call_kwargs["messages"]
    summary_prompt = messages[-1]["content"]
    assert "1.2.3.4" in summary_prompt
    assert "5.6.7.8" in summary_prompt


@pytest.mark.asyncio
async def test_summary_no_fabrication_instruction(agent, mock_gateway):
    """Summary prompt must instruct model not to invent details."""
    mock_gateway.chat_with_tools.return_value = GatewayResponse(
        finish_reason="stop", tool_calls=[], text="Done."
    )
    run_stats = {"hosts_new": 0, "hosts_updated": 0, "hosts_gone": 0}
    await agent._get_summary([], run_stats, new_discoveries=[])

    call_kwargs = mock_gateway.chat_with_tools.call_args.kwargs
    messages = call_kwargs["messages"]
    prompt = messages[-1]["content"]
    prompt_lower = prompt.lower()
    assert (
        "only" in prompt_lower
        or "do not invent" in prompt_lower
        or "summarise only" in prompt_lower
        or "summarize only" in prompt_lower
    )


@pytest.mark.asyncio
async def test_summary_empty_discoveries_produces_valid_response(agent, mock_gateway):
    """Empty discoveries list → method returns gateway text."""
    mock_gateway.chat_with_tools.return_value = GatewayResponse(
        finish_reason="stop", tool_calls=[], text="No new hosts found."
    )
    run_stats = {"hosts_new": 0, "hosts_updated": 0, "hosts_gone": 0}
    result = await agent._get_summary([], run_stats, new_discoveries=[])
    assert result == "No new hosts found."


@pytest.mark.asyncio
async def test_summary_includes_verified_stats_with_discoveries(agent, mock_gateway):
    """Both verified stats and structured discoveries appear in summary prompt."""
    mock_gateway.chat_with_tools.return_value = GatewayResponse(
        finish_reason="stop", tool_calls=[], text="Done."
    )
    discoveries = [{"ip": "10.0.0.1", "port": 8080, "service_type": "rpc", "is_new": True}]
    run_stats = {"hosts_new": 3, "hosts_updated": 1, "hosts_gone": 0}
    await agent._get_summary([], run_stats, new_discoveries=discoveries)

    call_kwargs = mock_gateway.chat_with_tools.call_args.kwargs
    messages = call_kwargs["messages"]
    prompt = messages[-1]["content"]
    assert "3" in prompt  # hosts_new
    assert "10.0.0.1" in prompt  # structured discovery
