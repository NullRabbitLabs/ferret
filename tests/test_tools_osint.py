"""
Tests for OSINT tools: GitHub code search and web search.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ============================================================
# GitHub Code Search
# ============================================================

@pytest.fixture
def github_tool_with_token():
    from src.tools.osint import GithubCodeSearchTool
    return GithubCodeSearchTool(github_token="ghp_test_token")


@pytest.fixture
def github_tool_no_token():
    from src.tools.osint import GithubCodeSearchTool
    return GithubCodeSearchTool(github_token=None)


GITHUB_API_RESPONSE = {
    "total_count": 2,
    "items": [
        {
            "repository": {"full_name": "operator/sui-validator"},
            "path": "config/validator.yaml",
            "html_url": "https://github.com/operator/sui-validator/blob/main/config/validator.yaml",
        },
        {
            "repository": {"full_name": "operator/infra"},
            "path": "ansible/roles/sui/vars.yaml",
            "html_url": "https://github.com/operator/infra/blob/main/ansible/roles/sui/vars.yaml",
        },
    ],
}


@pytest.mark.asyncio
async def test_github_search_returns_empty_without_token(github_tool_no_token):
    result = await github_tool_no_token.execute(query="sui validator config")
    assert result["results"] == []
    assert "note" in result
    assert "GITHUB_TOKEN" in result["note"]


@pytest.mark.asyncio
async def test_github_search_returns_results_with_token(github_tool_with_token):
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = GITHUB_API_RESPONSE

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_cls.return_value = mock_client

        result = await github_tool_with_token.execute(query="sui validator config")

    assert result["total_count"] == 2
    assert len(result["results"]) == 2
    assert result["results"][0]["repo"] == "operator/sui-validator"
    assert result["results"][0]["file_path"] == "config/validator.yaml"


@pytest.mark.asyncio
async def test_github_search_max_5_results(github_tool_with_token):
    many_items = [
        {
            "repository": {"full_name": f"org/repo{i}"},
            "path": f"file{i}.yaml",
            "html_url": f"https://github.com/org/repo{i}/file{i}.yaml",
        }
        for i in range(10)
    ]
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {"total_count": 10, "items": many_items}

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_cls.return_value = mock_client

        result = await github_tool_with_token.execute(query="test")

    assert len(result["results"]) <= 5


@pytest.mark.asyncio
async def test_github_search_adds_language_filter(github_tool_with_token):
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {"total_count": 0, "items": []}

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_cls.return_value = mock_client

        await github_tool_with_token.execute(query="sui validator", language="yaml")

    call_args = mock_client.get.call_args
    params = call_args[1]["params"]
    assert "language:yaml" in params["q"]


# ============================================================
# Web Search
# ============================================================

@pytest.fixture
def web_search_with_key():
    from src.tools.osint import WebSearchTool
    return WebSearchTool(serp_api_key="test_key")


@pytest.fixture
def web_search_no_key():
    from src.tools.osint import WebSearchTool
    return WebSearchTool(serp_api_key=None)


@pytest.mark.asyncio
async def test_web_search_returns_empty_without_key(web_search_no_key):
    result = await web_search_no_key.execute(query="sui validator operator")
    assert result["results"] == []
    assert "note" in result
    assert "SERP_API_KEY" in result["note"]


@pytest.mark.asyncio
async def test_web_search_returns_results_with_key(web_search_with_key):
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {
        "organic_results": [
            {"title": "Sui Validator Guide", "link": "https://example.com/guide", "snippet": "..."},
            {"title": "Operator Blog", "link": "https://operator.io/blog", "snippet": "..."},
        ]
    }

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_cls.return_value = mock_client

        result = await web_search_with_key.execute(query="sui validator operator")

    assert len(result["results"]) == 2
    assert result["results"][0]["title"] == "Sui Validator Guide"
    assert result["results"][0]["url"] == "https://example.com/guide"
