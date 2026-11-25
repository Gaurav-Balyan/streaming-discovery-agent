"""
The `discover` operation (ADR-004): Agent B's MODEL (Groq, via the OpenAI Agents SDK) chooses which
catalog searches to run for a free-text question. This is what makes Agent B an agent, not an API.

PROVENANCE (requirements B1/B3)
    The structured result is built by CODE from the outputs of the catalog tools that really ran in this
    task, recorded by RunHooks.on_tool_end. The model's final text is never parsed: it becomes the
    separate `answer` artifact, display text that Agent A must not trust for ids.

LIMITS
    * Tools: only search_title and search_youtube from Agent B's own catalog-mcp (static allow-list).
    * DISCOVER_MAX_TURNS model turns (4, as Agent A's Catalog agent).
    * DISCOVER_TIMEOUT seconds for the whole operation, below Agent A's proposed 45 s.
    * If the turn limit is hit, what the searches found so far is still returned, with a code-written
      answer saying the search did not finish.

OUTCOMES
    ok                          searches ran (possibly none found anything)
    rate_limited                Groq rate limit (MODEL_RATE_LIMITED)
    provider_error              model not configured / unavailable / misbehaving (unknown tool),
                                catalog-mcp would not start, timeout
"""
import asyncio
import os
import json
import logging

import openai
from agents import Agent, ModelSettings, RunHooks, Runner
from agents.exceptions import MaxTurnsExceeded, ModelBehaviorError
from agents.mcp import MCPServerStdio, create_static_tool_filter

from discovery_agent import config, runtime
from discovery_agent.contract import Request, envelope, error
from discovery_agent.operations import MAX_MATCHES, MAX_VIDEOS, _source, clean_matches, clean_videos

log = logging.getLogger("discovery_agent.discover")

DISCOVER_MAX_TURNS = 4
DISCOVER_TIMEOUT = 40
MAX_ANSWER_CHARS = 2000
CATALOG_TOOLS = ["search_title", "search_youtube"]

INSTRUCTIONS = (
    runtime.INSTRUCTIONS + " "
    "To find where a movie or series streams, call search_title (add a year only if the question gives one). "
    "For a song, music video or anything wanted on YouTube, call search_youtube (song + artist works best). "
    "You may search more than once, for example for several titles the question names. "
    "Then answer in one to three plain sentences: what was found and on which service, or that nothing was "
    "found, or that the search failed. Never name a title, year or id that a search did not return. "
    "If a tool returns an error, say the search failed; never say 'not found' for an error."
)


class ToolRecorder(RunHooks):
    """Writes down, by code, what each catalog tool returned. Model text never reaches here."""

    def __init__(self):
        self.title_results: list[dict] = []
        self.video_results: list[dict] = []
        self.calls = 0

    async def on_tool_end(self, context, agent, tool, result) -> None:
        self.calls += 1
        name = getattr(tool, "name", "")
        if name not in CATALOG_TOOLS:
            return
        data = _tool_json(result)
        if not isinstance(data, dict):
            return
        if name == "search_title":
            self.title_results.extend(m for m in data.get("matches") or [] if isinstance(m, dict))
        else:
            self.video_results.extend(v for v in data.get("videos") or [] if isinstance(v, dict))


def _tool_json(result):
    """The JSON object a catalog tool returned. The SDK hands MCP results over as text, a content block
    ({"type": "text", "text": "..."}) or a list of blocks. Anything else, or an error message, is no data."""
    if isinstance(result, dict):
        result = result.get("text") if result.get("type") == "text" else None
    elif isinstance(result, list):
        result = "".join(b.get("text", "") for b in result if isinstance(b, dict) and b.get("type") == "text")
    if not isinstance(result, str):
        return None
    try:
        return json.loads(result)
    except ValueError:
        return None


def _unique_matches(matches: list[dict]) -> list[dict]:
    seen, out = set(), []
    for m in matches:
        key = tuple(sorted(m["ids"].items()))
        if key not in seen:
            seen.add(key)
            out.append(m)
    return out[:MAX_MATCHES]


def _unique_videos(videos: list[dict]) -> list[dict]:
    seen, out = set(), []
    for v in videos:
        if v["video_id"] not in seen:
            seen.add(v["video_id"])
            out.append(v)
    return out[:MAX_VIDEOS]


def catalog_server() -> MCPServerStdio:
    cmd = config.CATALOG_MCP_COMMAND
    return MCPServerStdio(
        params={"command": cmd[0], "args": list(cmd[1:]), "cwd": str(config.BASE_DIR), "env": dict(os.environ)},
        name="catalog", cache_tools_list=True, client_session_timeout_seconds=config.CATALOG_CALL_TIMEOUT,
        tool_filter=create_static_tool_filter(allowed_tool_names=CATALOG_TOOLS))


def _failure(req: Request, outcome: str, code: str, message: str) -> tuple[dict, None]:
    return envelope(req.operation, req.request_id, outcome, error=error(code, message)), None


async def discover(req: Request) -> tuple[dict, str | None]:
    """Returns (result envelope, answer text or None)."""
    missing = config.missing_for_model()
    if missing:
        return _failure(req, "provider_error", "MISCONFIGURED", f"the model is not configured ({', '.join(missing)})")
    recorder = ToolRecorder()
    finished = True
    try:
        async with catalog_server() as server:
            agent = Agent(name="discovery", instructions=INSTRUCTIONS, model=runtime.model(),
                          mcp_servers=[server], model_settings=ModelSettings(temperature=0))
            try:
                run = await asyncio.wait_for(
                    Runner.run(agent, req.params["question"], max_turns=DISCOVER_MAX_TURNS, hooks=recorder),
                    DISCOVER_TIMEOUT)
                answer = str(run.final_output or "")
            except MaxTurnsExceeded:
                finished = False
                answer = ("The search did not finish within its turn limit. "
                          "These are the results the searches returned so far.")
    except asyncio.TimeoutError:
        return _failure(req, "provider_error", "DISCOVER_TIMEOUT", f"discovery took longer than {DISCOVER_TIMEOUT}s")
    except ModelBehaviorError:
        # e.g. the model asked for a tool it was not given: the SDK refuses and NOTHING runs.
        log.warning("request_id=%s: the model misbehaved (e.g. an unknown tool); nothing was run", req.request_id)
        return _failure(req, "provider_error", "MODEL_BEHAVIOR", "the model asked for something it cannot do")
    except openai.RateLimitError:
        return _failure(req, "rate_limited", "MODEL_RATE_LIMITED", "the model provider rate limited this request")
    except (openai.APIConnectionError, openai.APITimeoutError, openai.InternalServerError):
        return _failure(req, "provider_error", "MODEL_UNAVAILABLE", "the model provider is not reachable right now")
    except openai.APIStatusError as e:
        return _failure(req, "provider_error", "MODEL_ERROR", f"the model provider refused the request (HTTP {e.status_code})")
    except Exception as e:  # noqa: BLE001   e.g. catalog-mcp would not start; details only in the log
        log.exception("request_id=%s: discover failed", req.request_id)
        return _failure(req, "provider_error", "DISCOVER_FAILED", f"discovery failed ({type(e).__name__})")

    matches = _unique_matches(clean_matches(recorder.title_results, req.request_id))
    videos = _unique_videos(clean_videos(recorder.video_results, len(recorder.video_results) or 1, req.request_id))
    provider = ("mixed" if recorder.title_results and recorder.video_results
                else "youtube" if recorder.video_results else "streaming-availability")
    log.info("request_id=%s discover: %d tool call(s), %d match(es), %d video(s), finished=%s",
             req.request_id, recorder.calls, len(matches), len(videos), finished)
    result = envelope(req.operation, req.request_id, "ok", {"matches": matches, "videos": videos},
                      source=_source(provider))
    return result, " ".join(answer.split())[:MAX_ANSWER_CHARS] or None
