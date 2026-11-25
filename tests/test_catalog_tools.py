"""catalog-mcp's tools answer every anticipated failure with a CODE the operations can map (no network)."""
import asyncio

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from discovery_agent import config, resilience
from discovery_agent.catalog import client, lookup, mcp_server, youtube


def tool_error(coro) -> str:
    with pytest.raises(ToolError) as e:
        asyncio.run(coro)
    return str(e.value)


def raising(exc):
    async def f(*a, **k):
        raise exc
    return f


def status_error(code):
    r = httpx.Request("GET", "https://x.invalid")
    return httpx.HTTPStatusError(str(code), request=r, response=httpx.Response(code, request=r))


@pytest.mark.parametrize("exc, code", [
    (status_error(429), "RATE_LIMITED"),
    (status_error(503), "CATALOG_UNAVAILABLE"),
    (status_error(403), "PROVIDER_ERROR"),
    (httpx.ConnectError("no route"), "CATALOG_UNAVAILABLE"),
    (resilience.CircuitOpen("catalog API", 42), "CATALOG_UNAVAILABLE"),
    (config.MissingSetting("RAPIDAPI_KEY is not set"), "MISCONFIGURED"),
])
def test_title_search_failures_carry_a_code(monkeypatch, exc, code):
    monkeypatch.setattr(lookup, "find_title", raising(exc))
    assert tool_error(mcp_server.search_title("The Friend")).startswith(f"{code}:")


@pytest.mark.parametrize("args", [{"title": ""}, {"title": "x" * 201}, {"title": "Dune", "year": "20x5"}])
def test_bad_title_arguments_are_invalid_input(args):
    assert tool_error(mcp_server.search_title(**args)).startswith("INVALID_INPUT:")


@pytest.mark.parametrize("args", [{"query": "x"}, {"query": "ok query", "limit": 0}, {"query": "ok query", "limit": 6}])
def test_bad_youtube_arguments_are_invalid_input(args):
    assert tool_error(mcp_server.search_youtube(**args)).startswith("INVALID_INPUT:")


def test_a_broken_yt_dlp_is_youtube_search_failed(monkeypatch):
    def broken(q, limit):
        raise RuntimeError("unable to extract")
    monkeypatch.setattr(youtube, "find_videos", broken)
    assert tool_error(mcp_server.search_youtube("tum hi ho")).startswith("YOUTUBE_SEARCH_FAILED:")


@pytest.mark.parametrize("text, code", [
    ("Error executing tool search_title: RATE_LIMITED: slow down", "RATE_LIMITED"),
    ("INVALID_INPUT: bad", "INVALID_INPUT"),
    ("Error executing tool search_title", "CATALOG_ERROR"),                     # a crash: no internals
    ("Error executing tool search_title: KeyError 'secret_internal'", "CATALOG_ERROR"),
])
def test_the_client_reads_the_code_not_the_prose(text, code):
    e = client.parse_error(text)
    assert e.code == code
    assert "secret_internal" not in e.message


def test_a_missing_yt_dlp_is_misconfigured_not_a_youtube_change(monkeypatch):
    def not_installed(q, limit):
        raise ModuleNotFoundError("No module named 'yt_dlp'")
    monkeypatch.setattr(youtube, "find_videos", not_installed)
    msg = tool_error(mcp_server.search_youtube("tum hi ho"))
    assert msg.startswith("MISCONFIGURED:") and "requirements.txt" in msg


def test_readyz_reports_a_missing_yt_dlp(monkeypatch):
    import importlib.util
    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a: None if name == "yt_dlp" else real(name, *a))
    monkeypatch.setenv("RAPIDAPI_KEY", "x")
    assert config.missing_for_catalog() == ["yt-dlp (package)"]
