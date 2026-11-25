"""
Agent B's MCP client for its own catalog-mcp (stdio subprocess, ADR-006).

One session per A2A task: the subprocess starts, answers, and exits (as Agent A's McpToolbox does today).
Tool errors arrive as text; catalog-mcp starts every anticipated one with a CODE, after mcp's own
"Error executing tool <name>: " prefix. That code, not the prose, decides the contract outcome.
"""
import json
import os
import re
from contextlib import asynccontextmanager

from mcp import Client, StdioServerParameters

from discovery_agent import config

KNOWN_CODES = ("INVALID_INPUT", "RATE_LIMITED", "CATALOG_UNAVAILABLE", "PROVIDER_ERROR",
               "YOUTUBE_SEARCH_FAILED", "MISCONFIGURED")
_CODE = re.compile(r"^(?:Error executing tool \w+: )?(" + "|".join(KNOWN_CODES) + r"): ?(.*)$", re.S)


class CatalogError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code, self.message = code, " ".join(message.split())[:300]


def parse_error(text: str) -> CatalogError:
    m = _CODE.match(text.strip())
    if m:
        return CatalogError(m.group(1), m.group(2))
    # Not one of ours: a crash inside catalog-mcp. The details stay in its stderr, not in the reply.
    return CatalogError("CATALOG_ERROR", "the catalog service failed unexpectedly")


class CatalogSession:
    def __init__(self, client: Client):
        self._client = client

    async def call(self, tool: str, args: dict) -> dict:
        try:
            result = await self._client.call_tool(tool, args, read_timeout_seconds=config.CATALOG_CALL_TIMEOUT)
        except Exception as e:  # noqa: BLE001   protocol-level: the server died or timed out
            raise CatalogError("CATALOG_UNAVAILABLE", f"the catalog service did not answer ({type(e).__name__})") from e
        text = "\n".join(t for t in (getattr(b, "text", None) for b in result.content) if t)
        if result.is_error:
            raise parse_error(text)
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            raise CatalogError("CATALOG_ERROR", "the catalog service returned something that is not JSON") from None
        if not isinstance(data, dict):
            raise CatalogError("CATALOG_ERROR", "the catalog service returned an unexpected shape")
        return data


@asynccontextmanager
async def session():
    cmd = config.CATALOG_MCP_COMMAND
    params = StdioServerParameters(command=cmd[0], args=list(cmd[1:]), cwd=str(config.BASE_DIR),
                                   env=dict(os.environ))
    try:
        client_cm = Client(params)
        client = await client_cm.__aenter__()
    except Exception as e:  # noqa: BLE001   could not start or initialise the subprocess
        raise CatalogError("CATALOG_UNAVAILABLE", f"the catalog service could not start ({type(e).__name__})") from e
    try:
        yield CatalogSession(client)
    finally:
        try:
            await client_cm.__aexit__(None, None, None)
        except Exception:  # noqa: BLE001   closing a dead subprocess must not hide the real outcome
            pass
