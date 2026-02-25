"""
OSINT tools: GitHub code search and web search.
"""

import httpx

from src.tools.base import BaseTool
from src.tools.schemas import GITHUB_CODE_SEARCH_SCHEMA, WEB_SEARCH_SCHEMA


class GithubCodeSearchTool(BaseTool):
    """
    Search GitHub code via the GitHub Search API.

    Requires GITHUB_TOKEN to be configured.
    Rate limit: 10/minute → 0.167/s.
    Returns max 5 results.
    """

    rate_limit = 10 / 60  # 10 per minute

    def __init__(self, github_token: str | None = None) -> None:
        super().__init__()
        self._token = github_token

    @property
    def schema(self) -> dict:
        return GITHUB_CODE_SEARCH_SCHEMA

    async def execute(self, query: str, language: str | None = None, **kwargs) -> dict:
        if not self._token:
            return {
                "query": query,
                "results": [],
                "note": "GITHUB_TOKEN not configured — GitHub code search unavailable",
            }

        await self._rate_limit()
        search_query = query
        if language:
            search_query = f"{query} language:{language}"

        headers = {
            "Authorization": f"token {self._token}",
            "Accept": "application/vnd.github.v3+json",
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(
                    "https://api.github.com/search/code",
                    params={"q": search_query, "per_page": 5},
                    headers=headers,
                )
            response.raise_for_status()
            data = response.json()

            results = [
                {
                    "repo": item.get("repository", {}).get("full_name"),
                    "file_path": item.get("path"),
                    "url": item.get("html_url"),
                    "matched_lines": [],  # Full content not returned in search API
                }
                for item in data.get("items", [])[:5]
            ]
            return {"query": query, "total_count": data.get("total_count", 0), "results": results}
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 403:
                return {"query": query, "results": [], "error": "GitHub API rate limit exceeded"}
            return {"query": query, "results": [], "error": str(e)}
        except Exception as e:
            return {"query": query, "results": [], "error": str(e)}


class WebSearchTool(BaseTool):
    """
    Web search via SerpAPI (optional).

    Returns empty list with a note if SERP_API_KEY not configured.
    Rate limit: 5/minute → 0.083/s.
    """

    rate_limit = 5 / 60  # 5 per minute

    def __init__(self, serp_api_key: str | None = None) -> None:
        super().__init__()
        self._api_key = serp_api_key

    @property
    def schema(self) -> dict:
        return WEB_SEARCH_SCHEMA

    async def execute(self, query: str, **kwargs) -> dict:
        if not self._api_key:
            return {
                "query": query,
                "results": [],
                "note": "SERP_API_KEY not configured — web search unavailable",
            }

        await self._rate_limit()
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(
                    "https://serpapi.com/search",
                    params={
                        "q": query,
                        "api_key": self._api_key,
                        "engine": "google",
                        "num": 5,
                    },
                )
            response.raise_for_status()
            data = response.json()

            results = [
                {
                    "title": r.get("title"),
                    "url": r.get("link"),
                    "snippet": r.get("snippet"),
                }
                for r in data.get("organic_results", [])[:5]
            ]
            return {"query": query, "results": results}
        except Exception as e:
            return {"query": query, "results": [], "error": str(e)}
