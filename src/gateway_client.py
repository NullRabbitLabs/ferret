"""
Gateway client for Ferret.

Wraps /v1/chat/completions (OpenAI-format) for tool-augmented LLM calls.
"""

import asyncio
import json
from dataclasses import dataclass, field

import httpx

_MAX_RETRIES = 3
_RETRY_BACKOFF = [1.0, 2.0, 4.0]  # seconds between retries


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class GatewayResponse:
    finish_reason: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    text: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0


class DiscoveryGatewayClient:
    """
    HTTP client for the LLM gateway's /v1/chat/completions endpoint.

    Supports OpenAI-format tool use. Sends tools as the `tools` parameter.
    Reuses a single httpx.AsyncClient across calls — call close() when done.
    """

    def __init__(self, base_url: str, model: str = "deepseek-chat", timeout: float = 300.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout
        self._client = httpx.AsyncClient(timeout=self._timeout)

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        system_prompt: str | None = None,
    ) -> GatewayResponse:
        """
        POST to /v1/chat/completions with tools.

        If system_prompt is provided it is prepended as a system message.
        Returns GatewayResponse with finish_reason, tool_calls, text, and token counts.
        """
        full_messages = []
        if system_prompt:
            full_messages.append({"role": "system", "content": system_prompt})
        full_messages.extend(messages)

        payload = {
            "model": self._model,
            "messages": full_messages,
            "tools": tools,
        }

        last_error: Exception | None = None
        for attempt, backoff in enumerate((*_RETRY_BACKOFF, None)):
            response = await self._client.post(
                f"{self._base_url}/v1/chat/completions",
                json=payload,
            )
            try:
                response.raise_for_status()
                break
            except httpx.HTTPStatusError as e:
                if e.response.status_code < 500:
                    raise
                last_error = e
                if backoff is not None:
                    await asyncio.sleep(backoff)
        else:
            raise last_error  # type: ignore[misc]
        data = response.json()

        choice = data["choices"][0]
        message = choice["message"]
        finish_reason = choice.get("finish_reason", "stop")

        tool_calls: list[ToolCall] = []
        for tc in message.get("tool_calls") or []:
            raw_args = tc["function"].get("arguments", "{}")
            try:
                arguments = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except json.JSONDecodeError:
                arguments = {}
            tool_calls.append(
                ToolCall(
                    id=tc["id"],
                    name=tc["function"]["name"],
                    arguments=arguments,
                )
            )

        usage = data.get("usage", {})
        return GatewayResponse(
            finish_reason=finish_reason,
            tool_calls=tool_calls,
            text=message.get("content"),
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
        )

    async def get_embedding(self, text: str) -> list[float]:
        """POST to /embed to get a 1536-dimension embedding vector."""
        response = await self._client.post(
            f"{self._base_url}/embed",
            json={"input": text},
        )
        response.raise_for_status()
        data = response.json()
        # Handle both OpenAI embed format and raw list
        if isinstance(data, list):
            return data
        return data["data"][0]["embedding"]
