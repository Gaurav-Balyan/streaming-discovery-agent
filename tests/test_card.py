import asyncio

from a2a_helpers import URL, http

from discovery_agent.contract import CONTRACT, OPERATIONS


def get(path):
    async def go():
        async with http() as h:
            return await h.get(path)
    return asyncio.run(go())


def test_the_agent_card_is_retrievable_and_lists_one_skill_per_operation():
    r = get("/.well-known/agent-card.json")
    assert r.status_code == 200
    card = r.json()
    assert card["name"] == "streaming-discovery-agent"
    assert [s["id"] for s in card["skills"]] == list(OPERATIONS)
    assert all(f"contract:{CONTRACT}" in s["tags"] for s in card["skills"])
    assert card["supportedInterfaces"][0]["url"] == URL


def test_only_discover_is_marked_as_using_a_model():
    skills = {s["id"]: s["tags"] for s in get("/.well-known/agent-card.json").json()["skills"]}
    assert [op for op, tags in skills.items() if "model" in tags] == ["discover"]
    assert all("no-model" in skills[op] for op in ("lookup_title", "lookup_titles", "search_youtube"))


def test_the_card_says_it_cannot_play_and_holds_no_personal_data():
    d = get("/.well-known/agent-card.json").json()["description"]
    assert "Cannot play" in d and "no household or personal data" in d


def test_healthz():
    r = get("/healthz")
    assert r.status_code == 200 and r.json()["status"] == "ok" and r.json()["contract"] == CONTRACT


def test_readyz_names_missing_settings_but_never_values(monkeypatch):
    from discovery_agent import config
    monkeypatch.setattr(config, "GROQ_API_KEY", "")
    monkeypatch.setattr(config, "GROQ_MODEL", "some-model")
    monkeypatch.delenv("RAPIDAPI_KEY", raising=False)
    r = get("/readyz")
    assert r.status_code == 503 and r.json() == {"ready": False, "missing": ["GROQ_API_KEY", "RAPIDAPI_KEY"]}
    monkeypatch.setattr(config, "GROQ_API_KEY", "gsk_secret_value")
    monkeypatch.setenv("RAPIDAPI_KEY", "rapid_secret_value")
    r = get("/readyz")
    assert r.status_code == 200 and "secret_value" not in r.text
