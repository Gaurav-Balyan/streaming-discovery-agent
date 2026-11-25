"""
`discover` end to end: A2A -> executor -> OpenAI Agents SDK runner (scripted model) -> Agent B's
catalog-mcp (real server, fake providers) -> result built by CODE from the tool outputs.
"""
import fixtures as fx
import httpx
import openai
import pytest
from a2a.helpers import get_artifact_text
from a2a.types import TaskState
from a2a_helpers import data, result_of, send, status_text
from fake_model import ScriptedModel

from discovery_agent import config, contract, discover, runtime, skills


@pytest.fixture
def model(monkeypatch, catalog_mode):
    """Install a scripted model; returns a function that takes the script."""
    monkeypatch.setattr(config, "GROQ_API_KEY", "gsk_test")
    monkeypatch.setattr(config, "GROQ_MODEL", "test-model")
    catalog_mode("ok")
    holder = {}

    def script(*steps):
        holder["m"] = ScriptedModel(steps)
        monkeypatch.setattr(runtime, "model", lambda: holder["m"])
        return holder["m"]
    return script


def ask(question="Where is The Friend streaming?"):
    task = send(data({"contract": "discovery/1", "operation": "discover", "request_id": "t-2#1",
                      "params": {"question": question}}))
    assert task.status.state == TaskState.TASK_STATE_COMPLETED, status_text(task)
    r = result_of(task)
    contract.validate_result(r)
    by_name = {a.name: a for a in task.artifacts}
    answer = get_artifact_text(by_name["answer"]) if "answer" in by_name else None
    return r, answer


def test_discover_returns_what_the_search_returned_and_the_answer_separately(model):
    m = model([("search_title", {"title": "The Friend"})], "The Friend (2025) is on Netflix.")
    r, answer = ask()
    assert r["outcome"] == "ok"
    assert r["data"] == {"matches": [{"title": "The Friend", "year": "2025", "ids": {"netflix": fx.THE_FRIEND_NETFLIX}}],
                         "videos": []}
    assert answer == "The Friend (2025) is on Netflix."
    assert m.offered_tools[0] == ["search_title"]       # a title question: the title skill's tool only


def test_ids_in_the_model_text_are_never_results(model):
    """The model 'answers' with an id no search returned: it stays text, data has only searched ids."""
    model([("search_title", {"title": "The Friend"})],
          'Found it: {"matches": [{"title": "Evil", "year": "2024", "ids": {"netflix": "99999999"}}]}')
    r, answer = ask()
    assert "99999999" in answer
    assert [m["ids"] for m in r["data"]["matches"]] == [{"netflix": fx.THE_FRIEND_NETFLIX}]


def test_a_model_that_never_searches_returns_no_data(model):
    model("It is definitely on Netflix, id 12345.")
    r, answer = ask()
    assert r["data"] == {"matches": [], "videos": []} and "12345" in answer


def test_several_searches_are_merged_without_duplicates(model):
    model([("search_title", {"title": "Game Changer"}), ("search_title", {"title": "Game Changer", "year": "2025"})],
          [("search_youtube", {"query": "tum hi ho"})], "Found two Game Changers and a song.")
    r, _ = ask("Game Changer, and play tum hi ho")
    ids = [m["ids"] for m in r["data"]["matches"]]
    assert sorted(ids, key=str) == sorted([{"netflix": fx.GAME_CHANGER_2021_NETFLIX},
                                           {"prime": fx.GAME_CHANGER_2025_PRIME}], key=str)
    assert [v["video_id"] for v in r["data"]["videos"]] == [fx.YOUTUBE_TUM_HI_HO]
    assert r["source"]["provider"] == "mixed"


def test_an_injected_youtube_title_cannot_add_an_id(model):
    model([("search_youtube", {"query": "lofi study beats"})], f"Play {fx.INJECTED_ID} as the title says.")
    r, _ = ask("lofi study beats")
    assert [v["video_id"] for v in r["data"]["videos"]] == [fx.LOFI_FIRST, fx.LOFI_SECOND]


def test_the_turn_limit_still_returns_what_was_found(model):
    model(*([[("search_title", {"title": "The Friend"})]] * skills.registry().get("title-discovery").max_turns))
    r, answer = ask()
    assert r["outcome"] == "ok" and r["data"]["matches"][0]["ids"] == {"netflix": fx.THE_FRIEND_NETFLIX}
    assert answer.startswith("The search did not finish")


def test_a_failed_search_gives_no_data_and_the_model_is_told(model, catalog_mode):
    catalog_mode("rate_limited")
    m = model([("search_title", {"title": "The Friend"})], "The search failed.")
    r, answer = ask()
    assert r["outcome"] == "ok" and r["data"] == {"matches": [], "videos": []}
    assert "RATE_LIMITED" in str(m.inputs[-1])                   # the error text reached the model, not the data


def test_an_unknown_tool_name_from_the_model_does_not_run(model):
    """The model asks for play_title (Agent A's TV tool): the SDK refuses it and nothing runs."""
    model([("play_title", {"service": "netflix", "content_id": "1"})], "done")
    r, answer = ask("play it")
    assert r["outcome"] == "provider_error" and r["error"]["code"] == "MODEL_BEHAVIOR"
    assert r["data"] is None and answer is None


def _req():
    return httpx.Request("POST", "https://api.groq.test/chat")


@pytest.mark.parametrize("exc, outcome, code", [
    (openai.RateLimitError("slow", response=httpx.Response(429, request=_req()), body=None), "rate_limited",
     "MODEL_RATE_LIMITED"),
    (openai.APIConnectionError(request=_req()), "provider_error", "MODEL_UNAVAILABLE"),
    (openai.AuthenticationError("bad key", response=httpx.Response(401, request=_req()), body=None),
     "provider_error", "MODEL_ERROR"),
])
def test_model_provider_errors_map_to_outcomes(model, exc, outcome, code):
    model(exc)
    r, answer = ask()
    assert r["outcome"] == outcome and r["error"]["code"] == code and r["data"] is None and answer is None
    assert "bad key" not in r["error"]["message"]


def test_no_model_configuration_is_misconfigured(monkeypatch, catalog_mode):
    catalog_mode("ok")
    monkeypatch.setattr(config, "GROQ_API_KEY", "")
    r, _ = ask()
    assert r["outcome"] == "provider_error" and r["error"]["code"] == "MISCONFIGURED"
    assert "GROQ_API_KEY" in r["error"]["message"]


def test_a_catalog_server_that_will_not_start_is_a_provider_error(model, catalog_mode):
    catalog_mode("crash")
    model("unused")
    r, _ = ask()
    assert r["outcome"] == "provider_error" and r["error"]["code"] == "DISCOVER_FAILED"


def test_a_slow_discovery_times_out(model, monkeypatch):
    import asyncio

    class Slow(ScriptedModel):
        async def get_response(self, *a, **k):
            await asyncio.sleep(5)
    monkeypatch.setattr(discover, "DISCOVER_TIMEOUT", 0.5)
    model("unused")
    monkeypatch.setattr(runtime, "model", lambda: Slow([]))
    r, _ = ask()
    assert r["outcome"] == "provider_error" and r["error"]["code"] == "DISCOVER_TIMEOUT"


def test_the_answer_is_capped(model):
    model("x" * 5000)
    _, answer = ask()
    assert len(answer) == discover.MAX_ANSWER_CHARS
