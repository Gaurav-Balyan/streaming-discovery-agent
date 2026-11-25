"""Cache, retries, circuit breaker, stale-if-error. Ported from Agent A's tests/test_resilience.py (catalog part)."""
import asyncio
import contextlib
import io
import tempfile
from pathlib import Path

import httpx

from discovery_agent import config, resilience
from discovery_agent.catalog import lookup, mcp_server

FRIEND = "82182295"
SHOWS = [{"title": "The Friend", "releaseYear": 2025, "streamingOptions": {"in": [
    {"service": {"id": "netflix"}, "link": f"https://www.netflix.com/title/{FRIEND}"}]}}]


def http_error(status, headers=None):
    request = httpx.Request("GET", "https://example.invalid/search")
    return httpx.HTTPStatusError(f"{status}", request=request,
                                 response=httpx.Response(status, request=request, headers=headers or {}))


class FakeApi:
    def __init__(self, *script):
        self.script, self.calls = list(script), 0

    async def __call__(self, title):
        self.calls += 1
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, Exception):
            raise item
        return item


@contextlib.contextmanager
def catalog(api, **settings):
    keys = ("STATE_DIR", "CATALOG_BACKOFF", "CATALOG_CACHE_TTL", "CATALOG_STALE_MAX", "CATALOG_BREAKER_RESET")
    saved = {k: getattr(config, k) for k in keys}, lookup._api_search, lookup._stores
    config.STATE_DIR, config.CATALOG_BACKOFF = Path(tempfile.mkdtemp()), 0.01
    for k, v in settings.items():
        setattr(config, k, v)
    lookup._api_search, lookup._stores = api, None
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            yield
    finally:
        for k, v in saved[0].items():
            setattr(config, k, v)
        lookup._api_search, lookup._stores = saved[1], saved[2]


def test_transient_errors_are_retried_then_cached():
    api = FakeApi(http_error(503), httpx.ConnectTimeout("slow"), SHOWS)
    with catalog(api):
        first = asyncio.run(lookup.find_title("The Friend"))
        again = asyncio.run(lookup.find_title("the  FRIEND", "2025"))
    assert api.calls == 3 and first == again and first[0].ids == {"netflix": FRIEND}


def test_a_rate_limit_without_a_short_retry_after_is_not_retried():
    api = FakeApi(http_error(429))
    with catalog(api):
        try:
            asyncio.run(lookup.find_title("The Friend"))
        except httpx.HTTPStatusError:
            pass
    assert api.calls == 1


def test_a_rate_limit_with_a_short_retry_after_is_retried():
    api = FakeApi(http_error(429, {"Retry-After": "1"}), SHOWS)
    with catalog(api):
        found = asyncio.run(lookup.find_title("The Friend"))
    assert api.calls == 2 and found[0].ids == {"netflix": FRIEND}


def test_breaker_opens_after_repeated_failures_and_fails_fast():
    api = FakeApi(httpx.ConnectError("down"))
    with catalog(api):
        for _ in range(config.CATALOG_BREAKER_FAILURES):
            try:
                asyncio.run(lookup.find_title("The Friend"))
            except httpx.ConnectError:
                pass
        calls_before = api.calls
        try:
            asyncio.run(lookup.find_title("The Friend"))
            raise AssertionError("the breaker should be open")
        except resilience.CircuitOpen as e:
            open_error = e
        try:
            asyncio.run(mcp_server.search_title("The Friend"))
        except Exception as e:  # noqa: BLE001
            tool_error = str(e)
    assert calls_before == config.CATALOG_BREAKER_FAILURES * config.CATALOG_RETRIES
    assert api.calls == calls_before
    assert open_error.retry_in > 0
    assert tool_error.startswith("CATALOG_UNAVAILABLE:") and "not a missing title" in tool_error


def test_breaker_half_open_lets_one_call_through():
    now = [1000.0]
    b = resilience.CircuitBreaker("x", Path(tempfile.mkdtemp()) / "b.sqlite", failures=2, reset_after=60,
                                  clock=lambda: now[0])

    async def fail():
        raise OSError("down")

    async def ok():
        return "ok"
    for _ in range(2):
        try:
            asyncio.run(b.call(fail))
        except OSError:
            pass
    assert b.state()[0] == "open"
    now[0] += 61
    assert b.state()[0] == "half_open"
    try:
        asyncio.run(b.call(fail))
    except OSError:
        pass
    assert b.state()[0] == "open"
    now[0] += 61
    assert asyncio.run(b.call(ok)) == "ok" and b.state()[0] == "closed"


def test_when_the_api_is_down_an_older_cached_answer_is_used():
    api = FakeApi(SHOWS, httpx.ConnectError("down"))
    with catalog(api, CATALOG_CACHE_TTL=0):
        live = asyncio.run(lookup.find_title("The Friend"))
        stale = asyncio.run(lookup.find_title("The Friend"))
    assert api.calls == 1 + config.CATALOG_RETRIES and live == stale


def test_prime_counts_only_as_a_subscription():
    shows = [{"title": "Rent Only", "releaseYear": 2020, "streamingOptions": {"in": [
        {"service": {"id": "prime"}, "type": "rent",
         "link": "https://www.primevideo.com/detail?gti=amzn1.dv.gti.aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"}]}}]
    assert lookup.matches_from_shows(shows) == []
