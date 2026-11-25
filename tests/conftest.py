import sys
from pathlib import Path

import pytest

FAKE = Path(__file__).resolve().parent / "fake_catalog_server.py"


@pytest.fixture
def catalog_mode(monkeypatch, tmp_path):
    """Point Agent B's catalog-mcp subprocess at the fake server in `mode`, with its own temp state."""
    from discovery_agent import config

    def use(mode: str):
        monkeypatch.setattr(config, "CATALOG_MCP_COMMAND", [sys.executable, str(FAKE), mode])
        monkeypatch.setenv("STATE_DIR", str(tmp_path / "state"))
        monkeypatch.setenv("CATALOG_BACKOFF", "0.01")
        monkeypatch.setenv("RAPIDAPI_KEY", "test-not-a-real-key")
    return use
