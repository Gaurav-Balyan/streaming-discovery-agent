"""
The `discover` operation (ADR-004): Agent B's MODEL (Groq, via the OpenAI Agents SDK) chooses which
catalog searches to run for a free-text question. This is what makes Agent B an agent, not an API.

TOOLS (Phase 4, ADR-008)
    The model is given SDK FunctionTools built from config/mcp_tools.json: the approved name, the
    CONFIGURED description and input schema. Each call goes through tools.ToolExecutor (caller = the
    skill that owns the tool), the same controlled path as the no-model operations.

PROVENANCE (requirements B1/B3)
    The structured result is built by CODE from what the executor really returned, recorded inside the
    tool wrapper. The model's final text is never parsed: it becomes the separate `answer` artifact,
    display text that Agent A must not trust for ids.

SKILLS (Phase 3)
    skills.registry().route(question) picks the skill(s) by code. Only the selected skills' instructions
    are loaded (progressive disclosure), and only their tools are offered: the catalog-mcp tool filter is
    the union of the selected skills' `tools`. A tool outside it is refused by the SDK (MODEL_BEHAVIOR).

LIMITS
    * Turns: the largest `max_turns` among the selected skills.
    * DISCOVER_TIMEOUT seconds for the whole operation, below Agent A's proposed 45 s.
    * If the turn limit is hit, what the searches found so far is still returned, with a code-written
      answer saying the search did not finish.

OUTCOMES
    ok                          searches ran (possibly none found anything)
    rate_limited                Groq rate limit (MODEL_RATE_LIMITED)
    provider_error              model not configured / unavailable / misbehaving (unknown tool),
                                catalog-mcp would not start (CATALOG_UNAVAILABLE), timeout
"""
import asyncio
import os
import json
import logging

import openai
from agents import Agent, FunctionTool, ModelSettings, Runner
from agents.exceptions import MaxTurnsExceeded, ModelBehaviorError

from discovery_agent import config, runtime, skills, tools, tracing
from discovery_agent.catalog.client import CatalogError
from discovery_agent.contract import Request, envelope, error
from discovery_agent.operations import MAX_MATCHES, MAX_VIDEOS, _source, clean_matches, clean_videos

log = logging.getLogger("discovery_agent.discover")

DISCOVER_TIMEOUT = 40
MAX_ANSWER_CHARS = 2000

# What every discovery run is told, whatever skills are selected. Tool-specific guidance is in the skills.
BASE_INSTRUCTIONS = (
    runtime.INSTRUCTIONS + " "
    "Use only the tools you are given; you may call them more than once, e.g. for several titles. "
    "Then answer in one to three plain sentences: what was found and where, or that nothing was found, or "
    "that a search failed. Never name a title, year or id that a search did not return. If a tool returns "
    "an error, say the search failed; never say 'not found' for an error. If the question needs a search "
    "you have no tool for, say exactly: 'I cannot search that: the tool is not available'."
)


def plan(question: str) -> dict:
    """The skills for this question and everything derived from them (all by code)."""
    reg = skills.registry()
    names, reason = reg.route(question)
    metas = [reg.get(n) for n in names]
    tools = list(dict.fromkeys(t for m in metas for t in m.tools))
    instructions = BASE_INSTRUCTIONS + " " + " ".join(reg.instructions(n) for n in names)
    return {"skills": names, "reason": reason, "tools": tools, "max_turns": max(m.max_turns for m in metas),
            "instructions": instructions}


class ToolRecorder:
    """Writes down, by code, what each catalog tool returned. Model text never reaches here."""

    def __init__(self):
        self.title_results: list[dict] = []
        self.video_results: list[dict] = []
        self.calls = 0

    def record(self, tool: str, data: dict) -> None:
        if tool == "search_title":
            self.title_results.extend(m for m in data.get("matches") or [] if isinstance(m, dict))
        elif tool == "search_youtube":
            self.video_results.extend(v for v in data.get("videos") or [] if isinstance(v, dict))


def function_tools(route: dict, executor: tools.ToolExecutor, recorder: ToolRecorder,
                   permissions=tools.DEFAULT_PERMISSIONS) -> list[FunctionTool]:
    """One SDK tool per approved tool of the selected skills. What the model sees comes from the config."""
    out = []
    for tool in route["tools"]:
        spec = executor.cfg.tools[tool]
        caller = tools.Caller("skill", next(s for s in route["skills"] if s in spec.skills), frozenset(permissions))

        async def invoke(ctx, args_json: str, tool=tool, caller=caller) -> str:
            recorder.calls += 1
            try:
                args = json.loads(args_json or "{}")
            except ValueError:
                return json.dumps({"error": {"code": "INVALID_INPUT", "message": "arguments are not valid JSON"}})
            try:
                data = await executor.call(tool, args, caller)
            except CatalogError as e:
                return json.dumps({"error": {"code": e.code, "message": e.message}})
            recorder.record(tool, data)
            return json.dumps(data, ensure_ascii=False)

        out.append(FunctionTool(name=tool, description=spec.description, params_json_schema=spec.input_schema,
                                on_invoke_tool=invoke, strict_json_schema=False))
    return out


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


def _failure(req: Request, outcome: str, code: str, message: str) -> tuple[dict, None]:
    return envelope(req.operation, req.request_id, outcome, error=error(code, message)), None


async def discover(req: Request, permissions=tools.DEFAULT_PERMISSIONS) -> tuple[dict, str | None]:
    """Returns (result envelope, answer text or None)."""
    missing = config.missing_for_model()
    if missing:
        return _failure(req, "provider_error", "MISCONFIGURED", f"the model is not configured ({', '.join(missing)})")
    route = plan(req.params["question"])
    log.info("request_id=%s discover route: skills=%s (%s) tools=%s max_turns=%d instructions=%d chars",
             req.request_id, route["skills"], route["reason"], route["tools"], route["max_turns"],
             len(route["instructions"]))
    recorder = ToolRecorder()
    finished = True
    try:
        async with tools.ToolExecutor(tools.tool_config()) as executor:
            await executor.ensure(route["tools"])          # catalog-mcp cannot start -> provider_error
            agent = Agent(name="discovery", instructions=route["instructions"], model=runtime.model(),
                          tools=function_tools(route, executor, recorder, permissions),
                          model_settings=ModelSettings(temperature=0))
            try:
                with tracing.span("discover route", {"discover.skills": route["skills"],
                                                     "discover.route_reason": route["reason"],
                                                     "discover.tools": route["tools"]}):
                    run = await asyncio.wait_for(
                        Runner.run(agent, req.params["question"], max_turns=route["max_turns"]),
                        DISCOVER_TIMEOUT)
                answer = str(run.final_output or "")
            except MaxTurnsExceeded:
                finished = False
                answer = ("The search did not finish within its turn limit. "
                          "These are the results the searches returned so far.")
    except CatalogError as e:
        return _failure(req, "provider_error", e.code, e.message)
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
