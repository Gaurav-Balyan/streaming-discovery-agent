"""
The no-model discovery/1 operations (ADR-004): lookup_title, lookup_titles, search_youtube.

Agent B's CODE calls its catalog-mcp through the controlled executor (tools.py: approved tool, caller
allow-list, permission, configured schema, timeout) and builds the result. No model sees or writes any of it, so every
id in a result came from a real catalog tool result in THIS task (provenance requirement B1/B3).

Outcomes (contract §4): ok (empty = not found), invalid_input, rate_limited, provider_error.
Before anything is returned, results are cleaned (unknown services and malformed ids dropped, limits
applied); server.py then validates the whole envelope against the schema before sending it.
"""
import asyncio
import logging
import re
from datetime import datetime, timezone

from discovery_agent import config, tools
from discovery_agent.catalog import client as catalog
from discovery_agent.contract import Request, envelope, error

log = logging.getLogger("discovery_agent.operations")

MAX_MATCHES = 20
MAX_VIDEOS = 5
PARALLEL_SEARCHES = 4           # Agent A's check_titles limit: the provider's rate limit stays here, in B

# Same patterns as Agent A's device-mcp (ID_PATTERNS). Duplicated on purpose: independent checks.
ID_PATTERNS = {
    "netflix": re.compile(r"^\d{1,12}$"),
    "prime": re.compile(r"^amzn1\.dv\.gti\.[0-9a-fA-F-]{8,64}$"),
}
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
YEAR = re.compile(r"^\d{4}$")

OUTCOME_FOR_CODE = {"INVALID_INPUT": "invalid_input", "RATE_LIMITED": "rate_limited"}   # others: provider_error


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _source(provider: str) -> dict:
    return {"provider": provider, "country": config.COUNTRY, "retrieved_at": _now()}


def _text(value, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    return value[:limit] or None


def clean_matches(raw, request_id: str) -> list[dict]:
    out, dropped = [], 0
    for m in raw if isinstance(raw, list) else []:
        if len(out) == MAX_MATCHES:
            dropped += 1
            continue
        title = _text(m.get("title") if isinstance(m, dict) else None, 300)
        ids = m.get("ids") if isinstance(m, dict) else None
        ids = {s: str(i) for s, i in (ids or {}).items()
               if s in ID_PATTERNS and isinstance(i, (str, int)) and ID_PATTERNS[s].match(str(i))} \
            if isinstance(ids, dict) else {}
        if not title or not ids:
            dropped += 1
            continue
        year = str(m.get("year")) if m.get("year") is not None else None
        out.append({"title": title, "year": year if year and YEAR.match(year) else None, "ids": ids})
    if dropped:
        log.warning("request_id=%s: dropped %d match(es): over the limit, no title or no valid id", request_id, dropped)
    return out


def clean_videos(raw, limit: int, request_id: str) -> list[dict]:
    out, dropped = [], 0
    for v in raw if isinstance(raw, list) else []:
        vid = v.get("video_id") if isinstance(v, dict) else None
        title = _text(v.get("title") if isinstance(v, dict) else None, 300)
        if len(out) == limit or not isinstance(vid, str) or not VIDEO_ID.match(vid) or not title:
            dropped += 1
            continue
        duration = v.get("duration_seconds")
        out.append({"video_id": vid, "title": title, "channel": _text(v.get("channel"), 200),
                    "duration_seconds": int(duration) if isinstance(duration, (int, float)) and duration >= 0 else None})
    if dropped:
        log.warning("request_id=%s: dropped %d video(s): over the limit or malformed", request_id, dropped)
    return out


def _failure(req: Request, e: catalog.CatalogError) -> dict:
    return envelope(req.operation, req.request_id, OUTCOME_FOR_CODE.get(e.code, "provider_error"),
                    error=error(e.code, e.message))


def _title_args(p: dict) -> dict:
    return {"title": p["title"], **({"year": p["year"]} if p.get("year") else {})}


def _executor() -> tools.ToolExecutor:
    return tools.ToolExecutor(tools.tool_config())


async def lookup_title(req: Request, permissions=tools.DEFAULT_PERMISSIONS) -> dict:
    try:
        async with _executor() as ex:
            data = await ex.call("search_title", _title_args(req.params), tools.Caller("operation", req.operation, frozenset(permissions)))
    except catalog.CatalogError as e:
        return _failure(req, e)
    return envelope(req.operation, req.request_id, "ok", {"matches": clean_matches(data.get("matches"), req.request_id)},
                    source=_source("streaming-availability"))


async def lookup_titles(req: Request, permissions=tools.DEFAULT_PERMISSIONS) -> dict:
    gate = asyncio.Semaphore(PARALLEL_SEARCHES)

    caller = tools.Caller("operation", req.operation, frozenset(permissions))

    async def one(ex, t: dict) -> dict:
        item = {"title": t["title"], "year": t.get("year") or None}
        async with gate:
            try:
                data = await ex.call("search_title", _title_args(t), caller)
            except catalog.CatalogError as e:
                return {**item, "status": "error", "matches": [], "error": error(e.code, e.message)}
        matches = clean_matches(data.get("matches"), req.request_id)
        return {**item, "status": "found" if matches else "not_found", "matches": matches, "error": None}

    try:
        async with _executor() as ex:
            await ex.ensure(["search_title"])      # a server that cannot start fails the whole batch
            items = await asyncio.gather(*(one(ex, t) for t in req.params["titles"]))
    except catalog.CatalogError as e:
        return _failure(req, e)
    return envelope(req.operation, req.request_id, "ok", {"items": list(items)},
                    source=_source("streaming-availability"))


async def search_youtube(req: Request, permissions=tools.DEFAULT_PERMISSIONS) -> dict:
    limit = int(req.params.get("limit") or 3)       # A2A Structs carry numbers as floats: 3.0 -> 3
    try:
        async with _executor() as ex:
            data = await ex.call("search_youtube", {"query": req.params["query"], "limit": limit},
                                 tools.Caller("operation", req.operation, frozenset(permissions)))
    except catalog.CatalogError as e:
        return _failure(req, e)
    return envelope(req.operation, req.request_id, "ok",
                    {"videos": clean_videos(data.get("videos"), min(limit, MAX_VIDEOS), req.request_id)},
                    source=_source("youtube"))


HANDLERS = {"lookup_title": lookup_title, "lookup_titles": lookup_titles, "search_youtube": search_youtube}
