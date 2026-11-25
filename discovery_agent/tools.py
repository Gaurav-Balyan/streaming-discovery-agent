"""
Configuration-driven, controlled tool execution (Phase 4; plan tasks 4.1-4.8).

config/mcp_tools.json declares the approved tools; schemas/mcp-tools.schema.json is its schema.
Every catalog tool call in Agent B — from a no-model operation or from the model in `discover` — goes
through ToolExecutor.call, in this order:

    1. approved?        the tool is in the config                          else UNKNOWN_TOOL
    2. caller allowed?  the operation / skill is in the tool's allow-list  else TOOL_NOT_ALLOWED
    3. permission?      the caller holds the tool's permission             else PERMISSION_DENIED
    4. arguments?       valid against the CONFIGURED input schema          else INVALID_INPUT
    5. run              on the configured server, with the tool's timeout  else TOOL_TIMEOUT
    6. retry            only read tools, only listed codes, bounded

SAFETY RULES
    * A server is named by a HANDLER KEY. Code maps known keys to known commands (KNOWN_HANDLERS).
      The config cannot name a program, a path or Python code to run.
    * The model is shown the CONFIGURED description and schema, never what the server publishes, so a
      changed or hostile server description cannot reach the model.
    * At startup, verify_server() checks that every configured tool exists on the live server and that
      the configured schema fits the server's: same or fewer fields, the server's required fields
      required, compatible types. Server tools that are not configured are never offered.
    * Retries are allowed only for access "read" (no side effects), at most 3 attempts.

PERMISSIONS
    Phase 5: the executor's Caller carries the permissions of the authenticated A2A caller, from
    config/access.json (auth.py). DEFAULT_PERMISSIONS is only the default for direct calls in tests.
"""
import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from jsonschema import Draft202012Validator

from discovery_agent import config, skills
from discovery_agent.catalog import client as catalog
from discovery_agent.catalog.client import CatalogError
from discovery_agent.contract import OPERATIONS, first_problem

log = logging.getLogger("discovery_agent.tools")

CONFIG_FILE = config.BASE_DIR / "config" / "mcp_tools.json"
CONFIG_SCHEMA = json.loads((config.BASE_DIR / "schemas" / "mcp-tools.schema.json").read_text(encoding="utf-8"))
Draft202012Validator.check_schema(CONFIG_SCHEMA)

# The ONLY servers Agent B can start: handler key -> session factory. The config names a key; code owns
# the command (catalog.session starts config.CATALOG_MCP_COMMAND).
KNOWN_HANDLERS = {"catalog-mcp": catalog.session}

DEFAULT_PERMISSIONS = frozenset({"catalog:read"})        # Phase 5: from the caller's token + policy
NO_MODEL_OPERATIONS = {op for op, (needs_model, _) in OPERATIONS.items() if not needs_model}


class ToolConfigError(Exception):
    pass


@dataclass(frozen=True)
class ToolSpec:
    id: str
    server: str
    handler: str
    description: str
    access: str
    permission: str
    timeout_s: float
    max_attempts: int
    retry_on: frozenset
    operations: frozenset
    skills: frozenset
    input_schema: dict
    validator: Draft202012Validator = field(compare=False, repr=False)


@dataclass(frozen=True)
class Caller:
    kind: str                      # "operation" | "skill"
    name: str
    permissions: frozenset = DEFAULT_PERMISSIONS

    def __str__(self):
        return f"{self.kind}:{self.name}"


@dataclass(frozen=True)
class ToolConfig:
    version: str
    tools: dict

    def for_skill(self, skill: str) -> list[ToolSpec]:
        return [t for t in self.tools.values() if skill in t.skills]


def load(path: Path | None = None, registry: skills.Registry | None = None) -> ToolConfig:
    """Read, validate and cross-check the tool configuration. Any problem -> ToolConfigError."""
    path = path or CONFIG_FILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ToolConfigError(f"{path.name}: not found") from None
    except json.JSONDecodeError as e:
        raise ToolConfigError(f"{path.name}: not valid JSON ({e.msg}, line {e.lineno})") from None
    problem = first_problem(Draft202012Validator(CONFIG_SCHEMA), raw)
    if problem:
        raise ToolConfigError(f"{path.name}: {problem}")

    servers = raw["servers"]
    for name, s in servers.items():
        if s["handler"] not in KNOWN_HANDLERS:
            raise ToolConfigError(f"server {name!r}: unknown handler {s['handler']!r}; known: {sorted(KNOWN_HANDLERS)}")
    registry = registry or skills.registry()
    skill_names = {m.name for m in registry.metadata()}

    tools = {}
    for tid, t in raw["tools"].items():
        if t["server"] not in servers:
            raise ToolConfigError(f"tool {tid!r}: unknown server {t['server']!r}")
        if t["access"] != "read" and t["retry"]["max_attempts"] > 1:
            raise ToolConfigError(f"tool {tid!r}: only read tools may be retried (access is {t['access']!r})")
        if t["retry"]["max_attempts"] == 1 and t["retry"]["retry_on"]:
            raise ToolConfigError(f"tool {tid!r}: retry_on is set but max_attempts is 1")
        bad_ops = sorted(set(t["allowed"]["operations"]) - NO_MODEL_OPERATIONS)
        if bad_ops:
            raise ToolConfigError(f"tool {tid!r}: unknown or model operations {bad_ops} in allowed.operations")
        bad_skills = sorted(set(t["allowed"]["skills"]) - skill_names)
        if bad_skills:
            raise ToolConfigError(f"tool {tid!r}: unknown skills {bad_skills} in allowed.skills")
        try:
            Draft202012Validator.check_schema(t["input_schema"])
        except Exception as e:  # noqa: BLE001
            raise ToolConfigError(f"tool {tid!r}: input_schema is not a valid JSON Schema ({e})") from None
        unknown_required = sorted(set(t["input_schema"]["required"]) - set(t["input_schema"]["properties"]))
        if unknown_required:
            raise ToolConfigError(f"tool {tid!r}: required fields {unknown_required} are not in properties")
        tools[tid] = ToolSpec(
            id=tid, server=t["server"], handler=servers[t["server"]]["handler"], description=t["description"],
            access=t["access"], permission=t["permission"], timeout_s=float(t["timeout_s"]),
            max_attempts=t["retry"]["max_attempts"], retry_on=frozenset(t["retry"]["retry_on"]),
            operations=frozenset(t["allowed"]["operations"]), skills=frozenset(t["allowed"]["skills"]),
            input_schema=t["input_schema"], validator=Draft202012Validator(t["input_schema"]))

    # Skills and tools must agree both ways: a skill's tools are configured and allow that skill.
    for m in registry.metadata():
        for tool in m.tools:
            if tool not in tools:
                raise ToolConfigError(f"skill {m.name!r} uses {tool!r}, which is not an approved tool")
            if m.name not in tools[tool].skills:
                raise ToolConfigError(f"skill {m.name!r} uses {tool!r}, but the tool does not allow that skill")
    return ToolConfig(raw["version"], tools)


# ---------------------------------------------------------------- the live-server check
def _types(schema: dict) -> set:
    if "type" in schema:
        t = schema["type"]
        return set(t if isinstance(t, list) else [t])
    found = set()
    for key in ("anyOf", "oneOf"):
        for s in schema.get(key, []):
            found |= _types(s)
    return found


def schema_mismatches(tool: str, configured: dict, published: dict) -> list[str]:
    problems = []
    pub_props = published.get("properties", {})
    for name, prop in configured["properties"].items():
        if name not in pub_props:
            problems.append(f"{tool}.{name}: configured but the server has no such argument")
            continue
        want, have = _types(prop), _types(pub_props[name])
        if have and want and not want <= have:
            problems.append(f"{tool}.{name}: configured type {sorted(want)} not accepted by the server {sorted(have)}")
    missing = sorted(set(published.get("required", [])) - set(configured["required"]))
    if missing:
        problems.append(f"{tool}: the server requires {missing}, the configuration does not")
    return problems


def verify_published(cfg: ToolConfig, published: dict[str, dict]) -> list[str]:
    problems = []
    for t in cfg.tools.values():
        if t.id not in published:
            problems.append(f"{t.id}: configured but not offered by the server")
        else:
            problems += schema_mismatches(t.id, t.input_schema, published[t.id])
    extra = sorted(set(published) - set(cfg.tools))
    if extra:
        log.info("server tools not configured (never offered): %s", extra)
    return problems


async def verify_server(cfg: ToolConfig) -> list[str]:
    """Start each configured server once and compare. Returns problems (empty = all good)."""
    problems = []
    for handler in sorted({t.handler for t in cfg.tools.values()}):
        try:
            async with KNOWN_HANDLERS[handler]() as s:
                published = await s.list_tools()
        except CatalogError as e:
            problems.append(f"{handler}: could not start to verify tools ({e.code})")
            continue
        problems += verify_published(
            ToolConfig(cfg.version, {k: v for k, v in cfg.tools.items() if v.handler == handler}), published)
    return problems


# ---------------------------------------------------------------- the executor
class ToolExecutor:
    """One per task. Opens each server's session on first use; use `async with`."""

    def __init__(self, cfg: ToolConfig):
        self.cfg = cfg
        self._sessions: dict[str, object] = {}
        self._contexts: list = []
        self._lock = asyncio.Lock()           # parallel calls (lookup_titles) share ONE server process

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        for ctx in reversed(self._contexts):
            try:
                await ctx.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
        return False

    async def _session(self, spec: ToolSpec):
        async with self._lock:
            if spec.handler not in self._sessions:
                ctx = KNOWN_HANDLERS[spec.handler]()
                self._sessions[spec.handler] = await ctx.__aenter__()      # CatalogError if it cannot start
                self._contexts.append(ctx)
            return self._sessions[spec.handler]

    async def ensure(self, tool_ids) -> None:
        """Start the servers these tools need now, so a server that cannot start fails the whole
        operation (provider_error) instead of each call separately."""
        for tid in tool_ids:
            await self._session(self.cfg.tools[tid])

    def check(self, tool: str, args: dict, caller: Caller) -> ToolSpec:
        """Steps 1-4. Raises CatalogError; nothing has run yet."""
        spec = self.cfg.tools.get(tool)
        if spec is None:
            raise CatalogError("UNKNOWN_TOOL", f"{tool!r} is not an approved tool")
        allowed = spec.operations if caller.kind == "operation" else spec.skills if caller.kind == "skill" else ()
        if caller.name not in allowed:
            raise CatalogError("TOOL_NOT_ALLOWED", f"{caller} may not use {tool}")
        if spec.permission not in caller.permissions:
            raise CatalogError("PERMISSION_DENIED", f"{caller} lacks {spec.permission}")
        if not isinstance(args, dict):
            raise CatalogError("INVALID_INPUT", "arguments must be an object")
        problem = first_problem(spec.validator, args)
        if problem:
            raise CatalogError("INVALID_INPUT", problem)
        return spec

    async def call(self, tool: str, args: dict, caller: Caller) -> dict:
        spec = self.check(tool, args, caller)
        session = await self._session(spec)
        for attempt in range(1, spec.max_attempts + 1):
            try:
                return await asyncio.wait_for(session.call(tool, args), spec.timeout_s)
            except asyncio.TimeoutError:
                error = CatalogError("TOOL_TIMEOUT", f"{tool} took longer than {spec.timeout_s:g}s")
            except CatalogError as e:
                error = e
            if attempt < spec.max_attempts and error.code in spec.retry_on:
                log.warning("%s for %s: %s; attempt %d of %d", tool, caller, error.code, attempt, spec.max_attempts)
                continue
            raise error


_config: ToolConfig | None = None


def tool_config() -> ToolConfig:
    global _config
    if _config is None:
        _config = load()
    return _config
