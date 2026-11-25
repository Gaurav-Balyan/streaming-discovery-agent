"""
End to end inside Agent B: A2A message -> executor -> operation -> stdio catalog-mcp (fake) -> result.
Every result here also passed the schema check in server._complete (otherwise the task would be FAILED).
"""
import fixtures as fx
import pytest
from a2a.types import TaskState
from a2a_helpers import data, result_of, send, status_text

from discovery_agent import contract


def ask(operation, **params):
    task = send(data({"contract": "discovery/1", "operation": operation, "request_id": "t-1#1", "params": params}))
    assert task.status.state == TaskState.TASK_STATE_COMPLETED, status_text(task)
    r = result_of(task)
    contract.validate_result(r)          # what Agent A will check on receipt
    assert r["operation"] == operation and r["request_id"] == "t-1#1"
    return r


# ---------------------------------------------------------------- lookup_title
def test_lookup_title_finds_one_title(catalog_mode):
    catalog_mode("ok")
    r = ask("lookup_title", title="The Friend", year="2025")
    assert r["outcome"] == "ok" and r["error"] is None
    assert r["data"] == {"matches": [{"title": "The Friend", "year": "2025", "ids": {"netflix": fx.THE_FRIEND_NETFLIX}}]}
    assert r["source"]["provider"] == "streaming-availability" and r["source"]["country"] == "in"


def test_lookup_title_returns_both_services_and_several_matches(catalog_mode):
    catalog_mode("ok")
    assert ask("lookup_title", title="Dual Title")["data"]["matches"][0]["ids"] == {
        "netflix": fx.DUAL_NETFLIX, "prime": fx.DUAL_PRIME}
    years = sorted(m["year"] for m in ask("lookup_title", title="Game Changer")["data"]["matches"])
    assert years == ["2021", "2025"]


def test_not_found_is_ok_with_no_matches(catalog_mode):
    catalog_mode("ok")
    r = ask("lookup_title", title="zzzxqq")
    assert r["outcome"] == "ok" and r["data"] == {"matches": []}


def test_a_rate_limit_is_rate_limited_never_not_found(catalog_mode):
    catalog_mode("rate_limited")
    r = ask("lookup_title", title="The Friend")
    assert r["outcome"] == "rate_limited" and r["data"] is None and r["error"]["code"] == "RATE_LIMITED"


def test_an_outage_is_a_provider_error_after_the_real_retries(catalog_mode):
    catalog_mode("down")
    r = ask("lookup_title", title="The Friend")
    assert r["outcome"] == "provider_error" and r["error"]["code"] == "CATALOG_UNAVAILABLE"
    assert "not a missing title" in r["error"]["message"]


def test_a_catalog_server_that_dies_is_a_provider_error_not_a_crash(catalog_mode):
    catalog_mode("crash")
    r = ask("lookup_title", title="The Friend")
    assert r["outcome"] == "provider_error" and r["error"]["code"] == "CATALOG_UNAVAILABLE"


def test_junk_from_the_provider_is_cleaned_before_it_leaves_agent_b(catalog_mode):
    catalog_mode("bad_data")
    matches = ask("lookup_title", title="anything")["data"]["matches"]
    titles = [m["title"] for m in matches]
    assert len(matches) == 20                                        # capped
    assert "Wrong Service" not in titles and "Bad Netflix Id" not in titles and "" not in titles
    assert {"title": "Unknown Year", "year": None, "ids": {"prime": fx.DUAL_PRIME}} in matches   # "?" -> null
    assert {"title": "Half Good", "year": "2022", "ids": {"netflix": "55555"}} in matches          # bad prime id dropped
    assert all(set(m["ids"]) <= {"netflix", "prime"} for m in matches)


@pytest.mark.parametrize("params", [{}, {"title": ""}, {"title": "Dune", "year": "1Q84"}, {"title": "x" * 201}])
def test_bad_lookup_params_are_invalid_input_before_any_search(catalog_mode, params):
    catalog_mode("crash")                     # would fail if a search were attempted
    r = ask("lookup_title", **params)
    assert r["outcome"] == "invalid_input" and r["error"]["code"] == "INVALID_INPUT"


# ---------------------------------------------------------------- lookup_titles
def test_lookup_titles_reports_each_title(catalog_mode):
    catalog_mode("ok")
    r = ask("lookup_titles", titles=[{"title": "The Friend"}, {"title": "zzzxqq"}, {"title": "Lupin", "year": "2021"}])
    items = r["data"]["items"]
    assert [(i["title"], i["status"]) for i in items] == [("The Friend", "found"), ("zzzxqq", "not_found"),
                                                          ("Lupin", "found")]
    assert items[2]["year"] == "2021" and items[2]["matches"][0]["ids"] == {"netflix": fx.LUPIN_NETFLIX}


def test_lookup_titles_reports_per_title_errors_and_still_succeeds(catalog_mode):
    catalog_mode("rate_limited")
    r = ask("lookup_titles", titles=[{"title": "A"}, {"title": "B"}])
    assert r["outcome"] == "ok"
    assert [i["status"] for i in r["data"]["items"]] == ["error", "error"]
    assert r["data"]["items"][0]["error"]["code"] == "RATE_LIMITED"


def test_lookup_titles_takes_at_most_12(catalog_mode):
    catalog_mode("ok")
    assert len(ask("lookup_titles", titles=[{"title": f"T{i}"} for i in range(12)])["data"]["items"]) == 12
    r = ask("lookup_titles", titles=[{"title": f"T{i}"} for i in range(13)])
    assert r["outcome"] == "invalid_input" and "titles" in r["error"]["message"]


def test_lookup_titles_needs_at_least_one(catalog_mode):
    catalog_mode("ok")
    assert ask("lookup_titles", titles=[])["outcome"] == "invalid_input"


def test_lookup_titles_runs_at_most_four_searches_at_once(catalog_mode, monkeypatch):
    """The concurrency cap lives in Agent B, next to the RapidAPI key (ADR-004)."""
    import asyncio
    from discovery_agent import operations
    from discovery_agent.catalog import client
    running, peak = [0], [0]

    class Session:
        async def call(self, tool, args):
            running[0] += 1
            peak[0] = max(peak[0], running[0])
            await asyncio.sleep(0.02)
            running[0] -= 1
            return {"count": 0, "matches": []}

    class Ctx:
        async def __aenter__(self):
            return Session()

        async def __aexit__(self, *a):
            return False
    monkeypatch.setattr(client, "session", lambda: Ctx())
    r = ask("lookup_titles", titles=[{"title": f"T{i}"} for i in range(12)])
    assert r["outcome"] == "ok" and peak[0] == operations.PARALLEL_SEARCHES == 4


# ---------------------------------------------------------------- search_youtube
def test_search_youtube_finds_videos(catalog_mode):
    catalog_mode("ok")
    r = ask("search_youtube", query="tum hi ho arijit singh")
    assert r["outcome"] == "ok" and r["source"]["provider"] == "youtube"
    assert r["data"]["videos"] == [{"video_id": fx.YOUTUBE_TUM_HI_HO, "title": "Tum Hi Ho - Aashiqui 2 | Arijit Singh",
                                    "channel": "T-Series", "duration_seconds": 262}]


def test_search_youtube_limit_arrives_as_a_float_and_is_honoured(catalog_mode):
    catalog_mode("ok")
    assert len(ask("search_youtube", query="lofi study beats", limit=1)["data"]["videos"]) == 1


def test_an_injected_title_stays_a_title(catalog_mode):
    """A hostile upload's title is data. The id inside its text never becomes a video id."""
    catalog_mode("ok")
    videos = ask("search_youtube", query="lofi study beats")["data"]["videos"]
    assert [v["video_id"] for v in videos] == [fx.LOFI_FIRST, fx.LOFI_SECOND]
    assert fx.INJECTED_ID not in [v["video_id"] for v in videos]


def test_nothing_found_on_youtube_is_ok_and_empty(catalog_mode):
    catalog_mode("ok")
    assert ask("search_youtube", query="nothing like this")["data"] == {"videos": []}


def test_a_broken_youtube_search_is_a_provider_error(catalog_mode):
    catalog_mode("youtube_down")
    r = ask("search_youtube", query="tum hi ho")
    assert r["outcome"] == "provider_error" and r["error"]["code"] == "YOUTUBE_SEARCH_FAILED"


@pytest.mark.parametrize("params", [{"query": "x"}, {"query": "tum hi ho", "limit": 6}, {"query": "tum hi ho", "limit": 2.5}])
def test_bad_youtube_params_are_invalid_input(catalog_mode, params):
    catalog_mode("crash")
    assert ask("search_youtube", **params)["outcome"] == "invalid_input"


# ---------------------------------------------------------------- never send a broken result
def test_a_result_that_breaks_the_schema_is_never_sent(catalog_mode, monkeypatch):
    from discovery_agent import operations

    async def broken(req):
        return contract.envelope(req.operation, req.request_id, "ok", {"matches": [{"title": "X", "year": None,
                                 "ids": {"hbo": "1"}}]}, source={"provider": "streaming-availability",
                                 "country": "in", "retrieved_at": "2026-11-25T00:00:00Z"})
    monkeypatch.setitem(operations.HANDLERS, "lookup_title", broken)
    task = send(data({"contract": "discovery/1", "operation": "lookup_title", "request_id": "r", "params": {"title": "X"}}))
    assert task.status.state == TaskState.TASK_STATE_FAILED
    assert status_text(task) == "INTERNAL_ERROR: the result failed validation" and not task.artifacts
