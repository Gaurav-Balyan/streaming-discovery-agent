"""Phase 3: skill files, metadata-first registry, deterministic routing, tool scoping, disclosure size."""
from pathlib import Path

import fixtures as fx
import pytest
from a2a.helpers import get_artifact_text
from a2a.types import TaskState
from a2a_helpers import data, result_of, send, status_text
from fake_model import ScriptedModel

from discovery_agent import config, discover, runtime, skills

GOOD_HEADER = '''---
name: "{name}"
version: "1.0.0"
description: "A test skill."
tools: ["search_title"]
max_turns: 3
keywords: ["movie"]
---
Body of {name}.
'''


def write(tmp_path: Path, name: str, text: str | None = None) -> Path:
    p = tmp_path / f"{name}.md"
    p.write_text(text if text is not None else GOOD_HEADER.format(name=name), encoding="utf-8")
    return p


# ---------------------------------------------------------------- the shipped skills
def test_the_shipped_skills_load_and_each_owns_one_tool():
    reg = skills.Registry()
    assert {m.name: m.tools for m in reg.metadata()} == {"title-discovery": ("search_title",),
                                                         "youtube-discovery": ("search_youtube",)}


def test_metadata_is_read_without_any_instructions():
    reg = skills.Registry()
    assert reg.bodies_read == 0
    reg.instructions("youtube-discovery")
    reg.instructions("youtube-discovery")                 # cached
    assert reg.bodies_read == 1


@pytest.mark.parametrize("question, expected", [
    ("Where is The Friend (2025) streaming?", ["title-discovery"]),
    ("Is Game Changer a movie or a series?", ["title-discovery"]),
    ("Find on YouTube: tum hi ho arijit singh", ["youtube-discovery"]),           # Agent A's Supervisor phrasing
    ("play the song tum hi ho", ["youtube-discovery"]),
    ("Is Game Changer from 2025 on Prime, and find tum hi ho on YouTube", ["title-discovery", "youtube-discovery"]),
    ("The Friend 2025", ["title-discovery", "youtube-discovery"]),                 # no cue: all
    ("SHOWS like dark", ["title-discovery"]),                                      # case-insensitive
])
def test_routing(question, expected):
    assert skills.Registry().route(question)[0] == expected


def test_keywords_match_whole_words_only():
    """'showcase' is not 'show'; 'videogame' is not 'video'."""
    assert skills.Registry().route("showcase videogame")[1] == "no cue: all skills"


# ---------------------------------------------------------------- invalid skill files fail closed
@pytest.mark.parametrize("text, problem", [
    ("name: x\n", "must start with"),
    ("---\nname: \"bad\"\n", "not closed"),
    ("---\nname: bad\n---\nbody", "not JSON"),
    ('---\nname: "bad"\nversion: "1.0.0"\n---\nbody', "missing"),
    (GOOD_HEADER.format(name="other"), "match the file name"),
    (GOOD_HEADER.format(name="bad").replace('"1.0.0"', '"v1"'), "version"),
    (GOOD_HEADER.format(name="bad").replace('["search_title"]', '["play_title"]'), "unknown tools"),
    (GOOD_HEADER.format(name="bad").replace('["search_title"]', '[]'), "non-empty list"),
    (GOOD_HEADER.format(name="bad").replace("max_turns: 3", "max_turns: 50"), "max_turns"),
    (GOOD_HEADER.format(name="bad").replace("max_turns: 3", "max_turns: true"), "max_turns"),
    (GOOD_HEADER.format(name="bad").replace('["movie"]', '[""]'), "keywords"),
])
def test_an_invalid_skill_file_stops_the_registry(tmp_path, text, problem):
    write(tmp_path, "good")
    write(tmp_path, "bad", text)
    with pytest.raises(skills.SkillError, match=problem):
        skills.Registry(tmp_path)


def test_no_skill_files_is_an_error(tmp_path):
    with pytest.raises(skills.SkillError, match="no skill files"):
        skills.Registry(tmp_path)


def test_an_unknown_skill_name_is_an_error():
    with pytest.raises(skills.SkillError, match="unknown skill"):
        skills.Registry().instructions("play-anything")


def test_the_server_refuses_to_start_with_a_broken_skill(tmp_path, monkeypatch):
    from discovery_agent import server
    write(tmp_path, "bad", GOOD_HEADER.format(name="bad").replace('["search_title"]', '["play_title"]'))
    monkeypatch.setattr(skills, "SKILLS_DIR", tmp_path)
    monkeypatch.setattr(skills, "_registry", None)
    with pytest.raises(SystemExit, match="skill configuration error.*unknown tools"):
        server.main()


# ---------------------------------------------------------------- discover uses only the selected skills
@pytest.fixture
def scripted(monkeypatch, catalog_mode):
    monkeypatch.setattr(config, "GROQ_API_KEY", "gsk_test")
    monkeypatch.setattr(config, "GROQ_MODEL", "test-model")
    monkeypatch.setattr(skills, "_registry", None)
    catalog_mode("ok")
    seen = {}

    def script(*steps):
        seen["model"] = ScriptedModel(steps)
        monkeypatch.setattr(runtime, "model", lambda: seen["model"])
        return seen["model"]
    return script


def discover_task(question):
    return send(data({"contract": "discovery/1", "operation": "discover", "request_id": "t-3#1",
                      "params": {"question": question}}))


def test_a_youtube_question_is_offered_only_the_youtube_tool(scripted):
    m = scripted([("search_youtube", {"query": "tum hi ho"})], "Found it.")
    task = discover_task("Find on YouTube: tum hi ho")
    assert task.status.state == TaskState.TASK_STATE_COMPLETED
    assert m.offered_tools == [["search_youtube"], ["search_youtube"]]
    assert [v["video_id"] for v in result_of(task)["data"]["videos"]] == [fx.YOUTUBE_TUM_HI_HO]


def test_a_skill_cannot_use_another_skills_tool(scripted):
    """A YouTube question: the model tries search_title anyway. The SDK refuses; nothing runs."""
    scripted([("search_title", {"title": "The Friend"})], "done")
    r = result_of(discover_task("Find on YouTube: The Friend"))
    assert r["outcome"] == "provider_error" and r["error"]["code"] == "MODEL_BEHAVIOR" and r["data"] is None


def test_a_mixed_question_gets_both_tools_in_one_run(scripted):
    m = scripted([("search_title", {"title": "Game Changer", "year": "2025"}),
                  ("search_youtube", {"query": "tum hi ho"})], "Both found.")
    r = result_of(discover_task("Is Game Changer from 2025 on Prime, and find tum hi ho on YouTube"))
    assert m.offered_tools[0] == ["search_title", "search_youtube"]
    assert r["data"]["matches"][0]["ids"] == {"prime": fx.GAME_CHANGER_2025_PRIME}
    assert r["source"]["provider"] == "mixed"


def test_plan_loads_only_the_selected_skills(monkeypatch):
    reg = skills.Registry()
    monkeypatch.setattr(skills, "_registry", reg)
    p = discover.plan("Where is The Friend streaming?")
    assert p["skills"] == ["title-discovery"] and p["tools"] == ["search_title"] and p["max_turns"] == 4
    assert reg.instructions("title-discovery") in p["instructions"]
    assert "search_youtube" not in p["instructions"].replace(discover.BASE_INSTRUCTIONS, "")
    assert reg.bodies_read == 1                        # the YouTube skill's body was never read


def test_progressive_disclosure_saving_is_measured(monkeypatch, capsys):
    """Report the real numbers (plan task 3.8). The saving is small with two short skills: say so."""
    reg = skills.Registry()
    monkeypatch.setattr(skills, "_registry", reg)
    one = len(discover.plan("Where is The Friend streaming?")["instructions"])
    both = len(discover.plan("The Friend 2025")["instructions"])
    base = len(discover.BASE_INSTRUCTIONS)
    with capsys.disabled():
        print(f"\n[disclosure] base {base} chars; title-only {one}; all skills {both}; "
              f"saving {both - one} chars ({100 * (both - one) / both:.0f}%) for a single-skill question")
    assert base < one < both
