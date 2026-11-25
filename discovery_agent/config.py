"""
Agent B's own configuration. Read from this repository's .env and the environment, never from Agent A.

Only what the Streaming Discovery Agent needs. Phase 2 adds the catalog settings (RAPIDAPI_KEY, cache,
retries, breaker) when the catalog capability moves here.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

AGENT_NAME = "streaming-discovery-agent"
AGENT_VERSION = "0.1.0"

# ADR-003: Groq through the OpenAI Agents SDK (Chat Completions). Key separate from Agent A's.
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "")
GROQ_BASE_URL = os.environ.get("GROQ_BASE_URL", "https://api.groq.com/openai/v1")

# Until Phase 5 adds authentication (ADR-005) Agent B may listen on this PC only.
LOCAL_ONLY_HOSTS = {"127.0.0.1"}
HOST = os.environ.get("DISCOVERY_HOST", "127.0.0.1")
PORT = int(os.environ.get("DISCOVERY_PORT", "8010"))   # Agent A uses 8001-8004


def check_host(host: str) -> str:
    """Refuse any interface but loopback: there is no caller authentication yet (trust model §8)."""
    if host not in LOCAL_ONLY_HOSTS:
        raise SystemExit(f"DISCOVERY_HOST={host!r} refused: until Phase 5 (authentication) Agent B "
                         "listens on 127.0.0.1 only.")
    return host


def missing_for_model() -> list[str]:
    """Names (never values) of settings the model needs that are not set."""
    return [name for name, value in (("GROQ_API_KEY", GROQ_API_KEY), ("GROQ_MODEL", GROQ_MODEL)) if not value]
