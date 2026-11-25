# Streaming Discovery Agent (Agent B)

An independent A2A agent that finds where movies and series stream in India (Netflix, Prime Video) and
finds songs or videos on YouTube. It speaks the `discovery/1` contract. It cannot play anything and holds
no household or personal data.

Agent A (Household Watch Concierge, `../Watch Concierge`) will call it over A2A. Neither application
imports the other's code.

| | |
|---|---|
| Runtime | OpenAI Agents SDK, Groq as model provider via Chat Completions (ADR-003) |
| Interface | A2A 1.0 JSON-RPC (`a2a-sdk` 1.2.0), Agent Card at `/.well-known/agent-card.json` |
| Contract | `discovery/1`: `lookup_title`, `lookup_titles`, `search_youtube` (no model), `discover` (model) |
| Health | `/healthz` (up), `/readyz` (configuration complete) |
| Network | **127.0.0.1 only** until Phase 5 adds caller authentication (ADR-005) |

Design records (contract, trust model, ADRs) live in the "Streamwise Concierge" project.

## Status: Phase 3 (skills)

| Operation | Status |
|---|---|
| `lookup_title`, `lookup_titles`, `search_youtube` | Agent B's code calls its own catalog-mcp (stdio subprocess, ADR-006); no model. |
| `discover` | Groq model via the OpenAI Agents SDK chooses the searches (40 s). The `result` data is built by code from the tool outputs only; the model's prose is a separate, untrusted `answer` artifact. |

### Skills (`discovery_agent/skills/*.md`)

| Skill | Tools | Turns |
|---|---|---|
| `title-discovery` | `search_title` | 4 |
| `youtube-discovery` | `search_youtube` | 3 |

`discover` picks skills by code from each skill's `keywords` (no model call): one skill's cue → that
skill; several → all of them in one run; no cue → all. Only the selected skills' instructions are loaded
(the registry reads headers at startup, bodies on first use), and only their tools are offered. An
invalid skill file stops the server from starting.

Every request's params and every result are validated against `schemas/discovery-1.schema.json`
(the contract's source of truth). A result that breaks it is never sent.

The catalog (RapidAPI lookup with cache, retries, circuit breaker and stale fallback; YouTube via yt-dlp)
moved here from Agent A's `servers/catalog/`. Agent A keeps its own copy until Phase 6.

## Setup (Windows, Conda)

```powershell
conda create -n discovery-agent python=3.12 -y
conda activate discovery-agent
pip install -r requirements.txt
copy .env.example .env      # then fill in RAPIDAPI_KEY, GROQ_API_KEY (separate from Agent A's) and GROQ_MODEL
```

## Run

```powershell
python -m discovery_agent
# http://127.0.0.1:8010/healthz
# http://127.0.0.1:8010/.well-known/agent-card.json
```

Try it against the real providers (in a second terminal, server running):

```powershell
python scripts/ask.py lookup_title "The Friend" 2025
python scripts/ask.py lookup_titles "Inception" "Dune" "zzzxqq"
python scripts/ask.py search_youtube "tum hi ho arijit singh"
python scripts/ask.py discover "Is Game Changer from 2025 on Prime, and find tum hi ho on YouTube"
```

## Test

```powershell
python -m pytest -q
```

The tests need no network, no keys and no Agent A: the A2A app runs in-process and the catalog-mcp
subprocess is the real server with fake providers (`tests/fake_catalog_server.py`).
