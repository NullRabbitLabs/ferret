"""
Tests for DiscoveryGatewayClient.

Mocks the internal _client directly since the AsyncClient is now
reused across calls (created in __init__, not per-call).
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.gateway_client import DiscoveryGatewayClient


@pytest.fixture
def client():
    return DiscoveryGatewayClient(base_url="http://llm-gateway:8090")


def _make_mock_response(data: dict) -> MagicMock:
    mock = MagicMock()
    mock.raise_for_status = MagicMock()
    mock.json.return_value = data
    return mock


def _chat_response(finish_reason="stop", content="Done.", tool_calls=None, usage=None):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {
        "choices": [{"message": msg, "finish_reason": finish_reason}],
        "usage": usage or {"prompt_tokens": 100, "completion_tokens": 50},
    }


@pytest.mark.asyncio
async def test_chat_with_tools_sends_correct_payload(client):
    mock_response = _make_mock_response(_chat_response())
    client._client.post = AsyncMock(return_value=mock_response)

    await client.chat_with_tools(
        messages=[{"role": "user", "content": "Hello"}],
        tools=[{"type": "function", "function": {"name": "test"}}],
        system_prompt="You are a helpful agent.",
    )

    call_args = client._client.post.call_args
    url = call_args[0][0]
    payload = call_args[1]["json"]

    assert "/v1/chat/completions" in url
    assert payload["model"] == "deepseek-chat"
    assert payload["messages"][0]["role"] == "system"
    assert payload["messages"][0]["content"] == "You are a helpful agent."
    assert payload["messages"][1]["role"] == "user"
    assert len(payload["tools"]) == 1


@pytest.mark.asyncio
async def test_chat_with_tools_no_system_prompt(client):
    mock_response = _make_mock_response(_chat_response())
    client._client.post = AsyncMock(return_value=mock_response)

    await client.chat_with_tools(
        messages=[{"role": "user", "content": "Hello"}],
        tools=[],
    )

    payload = client._client.post.call_args[1]["json"]
    assert payload["messages"][0]["role"] == "user"  # No system message prepended


@pytest.mark.asyncio
async def test_chat_with_tools_returns_stop_response(client):
    mock_response = _make_mock_response(_chat_response(finish_reason="stop", content="All done."))
    client._client.post = AsyncMock(return_value=mock_response)

    result = await client.chat_with_tools(messages=[], tools=[])

    assert result.finish_reason == "stop"
    assert result.text == "All done."
    assert result.tool_calls == []
    assert result.prompt_tokens == 100
    assert result.completion_tokens == 50


@pytest.mark.asyncio
async def test_chat_with_tools_parses_tool_calls(client):
    tool_calls_data = [
        {
            "id": "call_abc",
            "type": "function",
            "function": {
                "name": "dns_lookup",
                "arguments": json.dumps({"hostname": "example.com", "record_type": "A"}),
            },
        }
    ]
    mock_response = _make_mock_response(
        _chat_response(finish_reason="tool_calls", tool_calls=tool_calls_data)
    )
    client._client.post = AsyncMock(return_value=mock_response)

    result = await client.chat_with_tools(messages=[], tools=[])

    assert result.finish_reason == "tool_calls"
    assert len(result.tool_calls) == 1
    tc = result.tool_calls[0]
    assert tc.id == "call_abc"
    assert tc.name == "dns_lookup"
    assert tc.arguments["hostname"] == "example.com"
    assert tc.arguments["record_type"] == "A"


@pytest.mark.asyncio
async def test_get_embedding_returns_vector(client):
    mock_response = _make_mock_response(
        {"data": [{"embedding": [0.1, 0.2, 0.3] * 512}]}
    )
    client._client.post = AsyncMock(return_value=mock_response)

    embedding = await client.get_embedding("test hypothesis")

    assert len(embedding) == 1536
    call_url = client._client.post.call_args[0][0]
    assert "/embed" in call_url


@pytest.mark.asyncio
async def test_client_close_closes_http_client(client):
    """close() must delegate to the underlying httpx client."""
    client._client.aclose = AsyncMock()
    await client.close()
    client._client.aclose.assert_called_once()


@pytest.mark.asyncio
async def test_chat_with_tools_retries_on_500_and_succeeds(client):
    """500 errors must be retried; success on second attempt must be returned."""
    import httpx

    mock_500 = MagicMock()
    mock_500.status_code = 500
    error = httpx.HTTPStatusError("500", request=MagicMock(), response=mock_500)
    mock_500.raise_for_status.side_effect = error

    mock_ok = _make_mock_response(_chat_response(content="ok after retry"))

    client._client.post = AsyncMock(side_effect=[mock_500, mock_ok])

    result = await client.chat_with_tools(messages=[], tools=[])

    assert result.text == "ok after retry"
    assert client._client.post.call_count == 2


@pytest.mark.asyncio
async def test_chat_with_tools_raises_after_max_retries(client):
    """Persistent 500 errors must raise after exhausting retries."""
    import httpx

    mock_500 = MagicMock()
    mock_500.status_code = 500
    error = httpx.HTTPStatusError("500", request=MagicMock(), response=mock_500)
    mock_500.raise_for_status.side_effect = error

    client._client.post = AsyncMock(return_value=mock_500)

    with pytest.raises(httpx.HTTPStatusError):
        await client.chat_with_tools(messages=[], tools=[])
