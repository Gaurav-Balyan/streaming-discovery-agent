"""
Agent B's REAL catalog-mcp (same tools, validation and error codes) with the outside world swapped out.
Started by the tests as the stdio subprocess:   python tests/fake_catalog_server.py <mode>

    ok            fixed catalog + fixed YouTube results (fixtures.py)
    rate_limited  every title search hits a simulated HTTP 429
    down          the API does not answer; the REAL cache, retries and breaker run
    bad_data      the provider returns junk: unknown services, malformed ids, 25 matches, a "?" year
    youtube_down  yt-dlp fails
    crash         the server exits before it can answer
stdout is the MCP channel: never print() here.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import httpx  # noqa: E402

from discovery_agent.catalog import lookup, mcp_server, youtube  # noqa: E402
import fixtures  # noqa: E402

mode = sys.argv[1] if len(sys.argv) > 1 else "ok"

if mode == "crash":
    sys.exit(3)

youtube.find_videos = fixtures.find_videos
if mode == "ok":
    lookup.find_title = fixtures.search
elif mode == "rate_limited":
    async def limited(title, year=None, only=None):
        request = httpx.Request("GET", "https://test.invalid/search")
        raise httpx.HTTPStatusError("429", request=request, response=httpx.Response(429, request=request))
    lookup.find_title = limited
elif mode == "down":
    async def outage(title):
        raise httpx.ConnectError("simulated outage")
    lookup._api_search = outage
elif mode == "bad_data":
    async def junk(title, year=None, only=None):
        rows = [lookup.Match("Good One", "2020", {"netflix": "123456"}),
                lookup.Match("Unknown Year", "?", {"prime": fixtures.DUAL_PRIME}),
                lookup.Match("Wrong Service", "2021", {"hbo": "999"}),
                lookup.Match("Bad Netflix Id", "2021", {"netflix": "../../etc"}),
                lookup.Match("Half Good", "2022", {"netflix": "55555", "prime": "not-a-gti"}),
                lookup.Match("", "2022", {"netflix": "66666"})]
        rows += [lookup.Match(f"Filler {i}", "2019", {"netflix": str(1000 + i)}) for i in range(25)]
        return rows
    lookup.find_title = junk
elif mode == "youtube_down":
    def broken(query, limit=3):
        raise RuntimeError("yt-dlp: unable to extract")
    youtube.find_videos = broken
else:
    raise SystemExit(f"unknown mode {mode}")

mcp_server.mcp.run()
