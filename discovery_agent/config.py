"""
Agent B's own configuration. Read from this repository's .env and the environment, never from Agent A.

Only what the Streaming Discovery Agent needs. The catalog settings moved here from Agent A's
common/config.py in Phase 2 (same names and defaults).
"""
import os
import sys
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

# Phase 5 (ADR-005): callers authenticate with a Keycloak token for this audience.
OIDC_ISSUER = os.environ.get("OIDC_ISSUER", "http://localhost:8080/realms/household")
AUDIENCE = "discovery-agent"

# Agent B listens on this PC only until Phase 8 adds TLS; tokens alone are not enough on a network.
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


def missing_for_catalog() -> list[str]:
    """Names (never values) of what the catalog needs and does not have: RAPIDAPI_KEY for title lookups,
    the yt-dlp package for YouTube search."""
    import importlib.util
    missing = [] if os.environ.get("RAPIDAPI_KEY") else ["RAPIDAPI_KEY"]
    if importlib.util.find_spec("yt_dlp") is None:
        missing.append("yt-dlp (package)")
    return missing


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"{name}={raw!r} is not a whole number") from None


class MissingSetting(RuntimeError):
    pass


def require(name: str) -> str:
    """A setting that must exist. Read at call time, so tests and the catalog subprocess see the environment."""
    value = os.environ.get(name, "")
    if not value:
        raise MissingSetting(f"{name} is not set (see .env.example)")
    return value


# ---------------------------------------------------------------- catalog (Phase 2, from Agent A)
COUNTRY = "in"                                   # streaming availability country
STATE_DIR = Path(os.environ.get("STATE_DIR", str(BASE_DIR / ".state")))   # Agent B's own cache + breaker
CATALOG_CACHE_TTL = _int("CATALOG_CACHE_TTL", 24 * 3600)
CATALOG_STALE_MAX = _int("CATALOG_STALE_MAX", 7 * 24 * 3600)
CATALOG_RETRIES = _int("CATALOG_RETRIES", 3)
CATALOG_BACKOFF = float(os.environ.get("CATALOG_BACKOFF", "0.5"))
CATALOG_BREAKER_FAILURES = _int("CATALOG_BREAKER_FAILURES", 3)
CATALOG_BREAKER_RESET = _int("CATALOG_BREAKER_RESET", 60)

# ADR-006: catalog-mcp runs as a stdio subprocess of Agent B. Tests point this at a fake server script.
CATALOG_MCP_COMMAND = [sys.executable, "-m", "discovery_agent.catalog.mcp_server"]
CATALOG_CALL_TIMEOUT = _int("CATALOG_CALL_TIMEOUT", 60)   # seconds for one catalog tool call (incl. retries)
