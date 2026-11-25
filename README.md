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

## Status: Phase 1 (skeleton)

The server, Agent Card, contract envelope checks and the Groq runtime exist. Every operation answers
`NOT_IMPLEMENTED` (a FAILED task) until Phase 2 moves the catalog capability here.

## Setup (Windows, Conda)

```powershell
conda create -n discovery-agent python=3.12 -y
conda activate discovery-agent
pip install -r requirements.txt
copy .env.example .env      # then fill in GROQ_API_KEY (a key separate from Agent A's) and GROQ_MODEL
```

## Run

```powershell
python -m discovery_agent
# http://127.0.0.1:8010/healthz
# http://127.0.0.1:8010/.well-known/agent-card.json
```

## Test

```powershell
python -m pytest -q
```

The tests need no network, no keys and no Agent A: the A2A app runs in-process.
