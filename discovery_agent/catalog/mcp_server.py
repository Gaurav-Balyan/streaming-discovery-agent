"""
catalog-mcp, owned by Agent B (moved from Agent A's servers/catalog/server.py in Phase 2).

Two tools: search_title (Streaming Availability via RapidAPI, India: Netflix + Prime subscription) and
search_youtube (yt-dlp). Same tools and behaviour as in Agent A, with two changes:

  * stdio ONLY (ADR-006). Agent B starts this as a subprocess; there is no network listener and so no
    token check. Agent A's HTTP mode was not carried over: re-add it with Agent B's own service identity
    (audience catalog-mcp) only if catalog-mcp ever runs as a separate deployment.
  * Every anticipated error message starts with a fixed CODE, so Agent B's code maps it to a contract
    outcome without reading prose:
        INVALID_INPUT        bad arguments                       -> outcome invalid_input
        RATE_LIMITED         provider HTTP 429                   -> outcome rate_limited
        CATALOG_UNAVAILABLE  circuit open / unreachable / 5xx    -> outcome provider_error
        PROVIDER_ERROR       other provider HTTP error           -> outcome provider_error
        YOUTUBE_SEARCH_FAILED yt-dlp failed                      -> outcome provider_error
        MISCONFIGURED        a required setting is missing       -> outcome provider_error
    Any other exception is a crash: the client sees only a generic error; details stay on stderr.

stdout is the MCP channel: never print() to it.
Run on its own (it waits for a client on stdin):  python -m discovery_agent.catalog.mcp_server
"""
import asyncio
import os
import dataclasses
import sys

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from discovery_agent import config, resilience
from discovery_agent.catalog import lookup, youtube

mcp = MCPServer("catalog")

MAX_VIDEOS = 5


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))
async def search_title(title: str, year: str | None = None) -> dict:
    """Search for a movie or series and return where it can be watched in India
    on Netflix or Prime Video (subscription only).

    Returns {"count": N, "matches": [...]}. Each match has title, year and ids
    (a dict of service -> content id). count 0 means the title was NOT FOUND on
    either service. A tool error means the lookup itself failed.

    Args:
        title: The title to search for.
        year: Optional 4-digit release year, to tell apart titles with the same name.
    """
    title = " ".join(title.split())
    if not 1 <= len(title) <= 200:
        raise ToolError("INVALID_INPUT: title must be 1 to 200 characters.")
    if year and not (len(year) == 4 and year.isdigit()):
        raise ToolError("INVALID_INPUT: year must be 4 digits, e.g. 2025.")
    try:
        matches = await lookup.find_title(title, year or None)   # models sometimes send "" for year
    except resilience.CircuitOpen as e:
        raise ToolError(f"CATALOG_UNAVAILABLE: lookups are paused for {e.retry_in:.0f}s after repeated "
                        "failures. This is a system problem, not a missing title.") from e
    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        if status == 429:
            raise ToolError("RATE_LIMITED: the catalog API rate limited this lookup (HTTP 429). "
                            "Retrying later may work; retrying immediately will not.") from e
        if status >= 500:
            raise ToolError(f"CATALOG_UNAVAILABLE: the catalog API failed (HTTP {status}).") from e
        raise ToolError(f"PROVIDER_ERROR: the catalog API refused the lookup (HTTP {status}).") from e
    except httpx.HTTPError as e:
        raise ToolError(f"CATALOG_UNAVAILABLE: the catalog API is unreachable ({type(e).__name__}). "
                        "This is a system problem, not a missing title.") from e
    except config.MissingSetting as e:
        raise ToolError(f"MISCONFIGURED: {e}") from e
    return {"count": len(matches), "matches": [dataclasses.asdict(m) for m in matches]}


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))
async def search_youtube(query: str, limit: int = 3) -> dict:
    """Search YouTube for a song, music video or other video, by name.

    Use it for songs and music ("tum hi ho by arijit singh") and anything wanted on YouTube.
    Not for movies and series on Netflix or Prime (use search_title).

    Returns {"count": N, "videos": [{"video_id", "title", "channel", "duration_seconds"}]},
    the top results in YouTube's order. count 0 means nothing was found.

    Args:
        query: What to search for, e.g. "tum hi ho arijit singh" (song + artist works best).
        limit: How many results, 1 to 5 (default 3).
    """
    query = " ".join(query.split())
    if not 2 <= len(query) <= 200:
        raise ToolError("INVALID_INPUT: query must be 2 to 200 characters, e.g. 'tum hi ho arijit singh'.")
    if not 1 <= limit <= MAX_VIDEOS:
        raise ToolError(f"INVALID_INPUT: limit must be 1 to {MAX_VIDEOS}.")
    try:
        videos = await asyncio.to_thread(youtube.find_videos, query, limit)
    except ImportError as e:              # yt-dlp is not installed: a setup problem, not a YouTube change
        raise ToolError("MISCONFIGURED: yt-dlp is not installed (pip install -r requirements.txt).") from e
    except Exception as e:  # noqa: BLE001   yt-dlp breaks when YouTube changes; say so plainly
        raise ToolError(f"YOUTUBE_SEARCH_FAILED: {type(e).__name__}. If this keeps happening, "
                        "update yt-dlp (pip install -U yt-dlp).") from e
    return {"count": len(videos), "videos": videos}


def main():
    # No RAPIDAPI_KEY is not fatal: search_youtube needs none. search_title answers MISCONFIGURED instead,
    # and Agent B's /readyz reports the missing setting.
    if not os.environ.get("RAPIDAPI_KEY"):
        print("catalog-mcp: RAPIDAPI_KEY is not set; search_title will answer MISCONFIGURED", file=sys.stderr)
    print("catalog-mcp (Agent B) starting on stdio", file=sys.stderr)
    mcp.run()


if __name__ == "__main__":
    main()
