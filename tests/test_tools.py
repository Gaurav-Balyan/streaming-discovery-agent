"""Phase 4: config/mcp_tools.json, the startup check against the live server, and the controlled executor."""
import asyncio
import copy
import json

import fixtures as fx
import pytest
from a2a.types import TaskState
from a2a_helpers import data, result_of, send
from fake_model import ScriptedModel

from discovery_agent import config, runtime, skills, tools
from discovery_agent.catalog.client import CatalogError

BASE = json.loads(tools.CONFIG_FILE.read_text(encoding="utf-8"))


def load_variant(tmp_path, change):
    cfg = copy.deepcopy(BASE)
    change(cfg)
    p = tmp_path / "mcp_tools.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    return tools.load(p, skills.Registry())


# ---------------------------------------------------------------- the shipped configuration
def test_the_shipped_configuration_loads():
    cfg = tools.load()
    assert sorted(cfg.tools) == ["search_title", "search_youtube"]
    t = cfg.tools["search_title"]
    assert t.access == "read" and t.permission == "catalog:read" and t.handler == "catalog-mcp"
    assert t.operations == {"lookup_title", "lookup_titles"} and t.skills == {"title-discovery"}


def test_the_shipped_configuration_matches_the_real_catalog_server(catalog_mode):
    catalog_mode("ok")                         # the real catalog-mcp tools, with fake providers
    assert asyncio.run(tools.verify_server(tools.load())) == []


# ---------------------------------------------------------------- invalid configuration fails closed
def _del(path):
    def change(c):
        *head, last = path
        node = c
        for k in head:
            node = node[k]
        del node[last]
    return change


@pytest.mark.parametrize("change, problem", [
    (lambda c: c.update(extra=1), "Additional properties"),
    (lambda c: c["servers"]["catalog"].update(handler="python"), "unknown handler 'python'"),
    (lambda c: c["servers"]["catalog"].update(handler="/bin/sh"), "does not match"),       # not even a key
    (lambda c: c["servers"]["catalog"].update(command=["python", "evil.py"]), "Additional properties"),
    (lambda c: c["tools"]["search_title"].update(server="elsewhere"), "unknown server"),
    (lambda c: c["tools"]["search_title"].update(access="write",
                                                 retry={"max_attempts": 2, "retry_on": ["TOOL_TIMEOUT"]}),
     "only read tools may be retried"),
    (lambda c: c["tools"]["search_title"].update(retry={"max_attempts": 1, "retry_on": ["TOOL_TIMEOUT"]}),
     "retry_on is set"),
    (lambda c: c["tools"]["search_title"].update(retry={"max_attempts": 9, "retry_on": []}), "max_attempts"),
    (lambda c: c["tools"]["search_title"]["allowed"].update(operations=["discover"]), "model operations"),
    (lambda c: c["tools"]["search_title"]["allowed"].update(skills=["ghost-skill"]), "unknown skills"),
    (lambda c: c["tools"]["search_title"]["input_schema"].update(additionalProperties=True), "additionalProperties"),
    (lambda c: c["tools"]["search_title"]["input_schema"].update(required=["title", "nope"]), "not in properties"),
    (lambda c: c["tools"]["search_title"]["input_schema"]["properties"]["title"].update(type="strng"),
     "not a valid JSON Schema"),
    (lambda c: c["tools"]["search_title"].update(timeout_s=0), "timeout_s"),
    (_del(["tools", "search_youtube"]), "not an approved tool"),               # a skill uses a missing tool
    (lambda c: c["tools"]["search_youtube"]["allowed"].update(skills=[]), "does not allow that skill"),
])
def test_an_invalid_configuration_is_refused(tmp_path, change, problem):
    with pytest.raises(tools.ToolConfigError, match=problem):
        load_variant(tmp_path, change)


def test_missing_and_unparsable_files(tmp_path):
    with pytest.raises(tools.ToolConfigError, match="not found"):
        tools.load(tmp_path / "absent.json", skills.Registry())
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(tools.ToolConfigError, match="not valid JSON"):
        tools.load(tmp_path / "bad.json", skills.Registry())


# ---------------------------------------------------------------- the configuration vs the live server
PUBLISHED = {
    "search_title": {"type": "object", "properties": {"title": {"type": "string"},
                                                      "year": {"anyOf": [{"type": "string"}, {"type": "null"}]}},
                     "required": ["title"]},
    "search_youtube": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
                       "required": ["query"]},
}


def test_matching_server_tools_pass_and_extra_server_tools_are_ignored():
    published = {**PUBLISHED, "delete_everything": {"type": "object", "properties": {}}}
    assert tools.verify_published(tools.load(), published) == []


@pytest.mark.parametrize("change, problem", [
    (lambda p: p.pop("search_youtube"), "search_youtube: configured but not offered"),
    (lambda p: p["search_title"]["properties"].pop("year"), "search_title.year: configured but the server has no"),
    (lambda p: p["search_youtube"]["properties"].update(limit={"type": "string"}), "search_youtube.limit: configured type"),
    (lambda p: p["search_title"].update(required=["title", "year"]), "the server requires ['year']"),
])
def test_schema_drift_on_the_server_is_detected(change, problem):
    published = copy.deepcopy(PUBLISHED)
    change(published)
    assert any(problem in x for x in tools.verify_published(tools.load(), published))


def test_a_server_that_cannot_start_is_reported(catalog_mode):
    catalog_mode("crash")
    problems = asyncio.run(tools.verify_server(tools.load()))
    assert problems == ["catalog-mcp: could not start to verify tools (CATALOG_UNAVAILABLE)"]


def test_the_server_refuses_to_start_with_a_bad_tool_config(tmp_path, monkeypatch):
    from discovery_agent import server
    (tmp_path / "mcp_tools.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(tools, "CONFIG_FILE", tmp_path / "mcp_tools.json")
    monkeypatch.setattr(tools, "_config", None)
    with pytest.raises(SystemExit, match="tool configuration error"):
        server.main()


def test_the_server_refuses_to_start_when_the_live_tools_differ(monkeypatch):
    from discovery_agent import server

    async def drift(cfg):
        return ["search_title.year: configured but the server has no such argument"]
    monkeypatch.setattr(tools, "verify_server", drift)
    with pytest.raises(SystemExit, match="does not match the catalog server"):
        server.main()


# ---------------------------------------------------------------- the executor
class FakeSession:
    def __init__(self, *script):
        self.script, self.calls = list(script), []

    async def call(self, tool, args):
        self.calls.append((tool, args))
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, BaseException):
            raise item
        if item == "slow":
            await asyncio.sleep(5)
        return item


def run_call(spec_changes, session, tool, args, caller):
    cfg = tools.load()
    if spec_changes:
        import dataclasses
        cfg = tools.ToolConfig(cfg.version, {**cfg.tools, tool: dataclasses.replace(cfg.tools[tool], **spec_changes)})

    class Ctx:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *a):
            return False

    async def go():
        saved = tools.KNOWN_HANDLERS["catalog-mcp"]
        tools.KNOWN_HANDLERS["catalog-mcp"] = lambda: Ctx()
        try:
            async with tools.ToolExecutor(cfg) as ex:
                return await ex.call(tool, args, caller)
        finally:
            tools.KNOWN_HANDLERS["catalog-mcp"] = saved
    return asyncio.run(go())


OP = tools.Caller("operation", "lookup_title")


def code_of(fn):
    with pytest.raises(CatalogError) as e:
        fn()
    return e.value.code


@pytest.mark.parametrize("tool, args, caller, code", [
    ("play_title", {"service": "netflix"}, OP, "UNKNOWN_TOOL"),
    ("search_youtube", {"query": "abc"}, OP, "TOOL_NOT_ALLOWED"),                         # lookup_title may not
    ("search_title", {"title": "X"}, tools.Caller("skill", "youtube-discovery"), "TOOL_NOT_ALLOWED"),
    ("search_title", {"title": "X"}, tools.Caller("agent", "lookup_title"), "TOOL_NOT_ALLOWED"),   # unknown kind
    ("search_title", {"title": "X"}, tools.Caller("operation", "lookup_title", frozenset()), "PERMISSION_DENIED"),
    ("search_title", {"title": ""}, OP, "INVALID_INPUT"),
    ("search_title", {"title": "X", "year": "20x5"}, OP, "INVALID_INPUT"),
    ("search_title", {"title": "X", "user": "gaurav"}, OP, "INVALID_INPUT"),              # no identity smuggling
    ("search_title", "The Friend", OP, "INVALID_INPUT"),
])
def test_refused_calls_never_reach_the_server(tool, args, caller, code):
    session = FakeSession({"count": 0, "matches": []})
    assert code_of(lambda: run_call(None, session, tool, args, caller)) == code
    assert session.calls == []


def test_an_allowed_call_reaches_the_server():
    session = FakeSession({"count": 0, "matches": []})
    assert run_call(None, session, "search_title", {"title": "X"}, OP) == {"count": 0, "matches": []}
    assert session.calls == [("search_title", {"title": "X"})]


def test_a_slow_tool_times_out():
    assert code_of(lambda: run_call({"timeout_s": 0.2}, FakeSession("slow"), "search_title", {"title": "X"}, OP)) \
        == "TOOL_TIMEOUT"


def test_a_read_tool_is_retried_only_for_listed_codes():
    flaky = FakeSession(CatalogError("CATALOG_UNAVAILABLE", "x"), {"count": 0, "matches": []})
    changes = {"max_attempts": 2, "retry_on": frozenset({"CATALOG_UNAVAILABLE"})}
    assert run_call(changes, flaky, "search_title", {"title": "X"}, OP)["count"] == 0
    assert len(flaky.calls) == 2
    limited = FakeSession(CatalogError("RATE_LIMITED", "x"), {"count": 0, "matches": []})
    assert code_of(lambda: run_call(changes, limited, "search_title", {"title": "X"}, OP)) == "RATE_LIMITED"
    assert len(limited.calls) == 1                     # not listed: no retry


def test_retries_are_bounded():
    down = FakeSession(CatalogError("CATALOG_UNAVAILABLE", "x"))
    changes = {"max_attempts": 3, "retry_on": frozenset({"CATALOG_UNAVAILABLE"})}
    assert code_of(lambda: run_call(changes, down, "search_title", {"title": "X"}, OP)) == "CATALOG_UNAVAILABLE"
    assert len(down.calls) == 3


def test_the_shipped_tools_are_not_retried_at_tool_level():
    """lookup.py already retries the provider (3 attempts); a tool-level retry would multiply them."""
    assert all(t.max_attempts == 1 for t in tools.load().tools.values())


# ---------------------------------------------------------------- what the model sees comes from the config
@pytest.fixture
def scripted(monkeypatch, catalog_mode):
    monkeypatch.setattr(config, "GROQ_API_KEY", "gsk_test")
    monkeypatch.setattr(config, "GROQ_MODEL", "test-model")
    catalog_mode("ok")
    holder = {}

    def script(*steps):
        holder["m"] = ScriptedModel(steps)
        monkeypatch.setattr(runtime, "model", lambda: holder["m"])
        return holder["m"]
    return script


def discover(question):
    return send(data({"contract": "discovery/1", "operation": "discover", "request_id": "t-4#1",
                      "params": {"question": question}}))


def test_the_model_sees_the_configured_description_and_schema_not_the_servers(scripted):
    m = scripted([("search_title", {"title": "The Friend"})], "ok")
    assert discover("Where is The Friend streaming?").status.state == TaskState.TASK_STATE_COMPLETED
    spec = tools.load().tools["search_title"]
    assert m.descriptions["search_title"] == spec.description
    assert m.schemas["search_title"]["additionalProperties"] is False
    assert set(m.schemas["search_title"]["properties"]) == {"title", "year"}


def test_bad_model_arguments_are_refused_before_the_server_and_the_model_is_told(scripted):
    m = scripted([("search_title", {"title": "The Friend", "year": "next year"})], "The search failed.")
    r = result_of(discover("Where is The Friend streaming?"))
    assert r["outcome"] == "ok" and r["data"]["matches"] == []
    assert "INVALID_INPUT" in str(m.inputs[-1])


def test_unparsable_model_arguments_are_refused(scripted, monkeypatch):
    from discovery_agent import discover as d
    calls = []
    real = d.function_tools

    def spy(route, executor, recorder, *rest):
        made = real(route, executor, recorder, *rest)
        calls.extend(made)
        return made
    monkeypatch.setattr(d, "function_tools", spy)
    scripted("unused")
    discover("Where is The Friend streaming?")
    out = asyncio.run(calls[0].on_invoke_tool(None, "{not json"))
    assert json.loads(out)["error"]["code"] == "INVALID_INPUT"


def test_discover_and_the_lookups_share_the_same_controlled_path(scripted):
    """The model's search goes through the executor with caller skill:title-discovery."""
    scripted([("search_title", {"title": "The Friend"})], "ok")
    r = result_of(discover("Where is The Friend streaming?"))
    assert r["data"]["matches"][0]["ids"] == {"netflix": fx.THE_FRIEND_NETFLIX}
