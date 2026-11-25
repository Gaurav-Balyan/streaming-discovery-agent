"""
Send one discovery/1 request to a RUNNING Agent B and print the task state and result.
A manual check with the real providers (RapidAPI, YouTube); the tests never touch them.

Since Phase 5 Agent B needs a token. This script stands in for Agent A: it gets one from Keycloak with
the concierge-agent client (client-credentials). The secret is Agent A's, so it is read from
CONCIERGE_AGENT_CLIENT_SECRET, defaulting to the dev value in Agent A's keycloak/household-realm.json.
    python scripts/ask.py --no-token lookup_title "The Friend"      # see the 401

    python scripts/ask.py lookup_title "The Friend" 2025
    python scripts/ask.py lookup_titles "Inception" "Dune" "zzzxqq"
    python scripts/ask.py search_youtube "tum hi ho arijit singh"
    python scripts/ask.py discover "Is Game Changer from 2025 on Prime, and find tum hi ho on YouTube"
"""
import asyncio
import json
import os
import sys
import uuid

import httpx
from a2a.client import A2ACardResolver, ClientConfig, create_client
from a2a.helpers import get_artifact_text, get_data_parts, get_message_text, new_data_part
from a2a.types import Message, Role, SendMessageRequest, TaskState

URL = "http://127.0.0.1:8010"
ISSUER = os.environ.get("OIDC_ISSUER", "http://localhost:8080/realms/household")


def service_token() -> str:
    r = httpx.post(f"{ISSUER}/protocol/openid-connect/token", timeout=10, data={
        "grant_type": "client_credentials", "client_id": "concierge-agent",
        "client_secret": os.environ.get("CONCIERGE_AGENT_CLIENT_SECRET", "concierge-agent-dev-secret")})
    if r.status_code != 200:
        sys.exit(f"Keycloak refused the concierge-agent token ({r.status_code}): {r.text[:200]}")
    return r.json()["access_token"]


def params(op: str, args: list[str]) -> dict:
    if op == "lookup_title":
        return {"title": args[0], **({"year": args[1]} if len(args) > 1 else {})}
    if op == "lookup_titles":
        return {"titles": [{"title": a} for a in args]}
    if op == "search_youtube":
        return {"query": " ".join(args)}
    if op == "discover":
        return {"question": " ".join(args)}
    raise SystemExit(f"unknown operation {op}")


async def main(op: str, args: list[str], with_token: bool = True):
    request = {"contract": "discovery/1", "operation": op, "request_id": f"manual-{uuid.uuid4().hex[:8]}",
               "params": params(op, args)}
    headers = {"Authorization": f"Bearer {service_token()}"} if with_token else {}
    async with httpx.AsyncClient(timeout=120, headers=headers) as http:
        card = await A2ACardResolver(httpx_client=http, base_url=URL).get_agent_card()
        client = await create_client(agent=card, client_config=ClientConfig(streaming=False, httpx_client=http))
        if not with_token:
            r = await http.post(URL + "/", json={"jsonrpc": "2.0", "id": "1", "method": "SendMessage", "params": {}})
            print("HTTP", r.status_code, r.text)
            return
        task = None
        try:
            msg = Message(role=Role.ROLE_USER, message_id=uuid.uuid4().hex,
                          parts=[new_data_part(request, media_type="application/json")])
            async for chunk in client.send_message(SendMessageRequest(message=msg)):
                if chunk.HasField("task"):
                    task = chunk.task
        finally:
            await client.close()
    print("task:", TaskState.Name(task.status.state))
    if task.status.HasField("message"):
        print("status:", get_message_text(task.status.message))
    for a in task.artifacts:
        values = get_data_parts(a.parts)
        if values:
            print(f"artifact {a.name}:")
            print(json.dumps(values[0], indent=2, ensure_ascii=False))
        else:
            print(f"artifact {a.name} (text, untrusted): {get_artifact_text(a)}")


if __name__ == "__main__":
    argv = sys.argv[1:]
    with_token = "--no-token" not in argv
    argv = [a for a in argv if a != "--no-token"]
    if len(argv) < 2:
        sys.exit(__doc__)
    asyncio.run(main(argv[0], argv[1:], with_token))
