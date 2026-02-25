"""
Base tool class with rate limiting via token bucket algorithm.
"""

import asyncio
import time
from abc import ABC, abstractmethod


class BaseTool(ABC):
    """
    Abstract base for all discovery tools.

    Provides a token bucket rate limiter. Subclasses set `rate_limit`
    (tokens per second) and call `await self._rate_limit()` before I/O.
    """

    rate_limit: float = 10.0  # tokens per second, override in subclass

    def __init__(self) -> None:
        self._tokens: float = self.rate_limit
        self._last_refill: float = time.monotonic()
        self._lock = asyncio.Lock()

    async def _rate_limit(self) -> None:
        """Block until a rate-limit token is available."""
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(
                self.rate_limit,
                self._tokens + elapsed * self.rate_limit,
            )
            self._last_refill = now

            if self._tokens < 1.0:
                wait = (1.0 - self._tokens) / self.rate_limit
                await asyncio.sleep(wait)
                self._tokens = 0.0
            else:
                self._tokens -= 1.0

    @abstractmethod
    async def execute(self, **kwargs) -> dict:
        """Execute the tool and return a JSON-serialisable result dict."""

    @property
    @abstractmethod
    def schema(self) -> dict:
        """Return the OpenAI-format tool schema for this tool."""
