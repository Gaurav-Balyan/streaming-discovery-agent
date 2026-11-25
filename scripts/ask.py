"""
Send one discovery/1 request to a RUNNING Agent B and print the task state and result.
A manual check with the real providers (RapidAPI, YouTube); the tests never touch them.

    python scripts/ask.py lookup_title "The Friend" 2025
    python scripts/ask.py lookup_titles "Inception" "Dune" "zzzxqq"
    python scripts/ask.py search_youtube "tum hi ho arijit singh"
"""
import asyncio
import json
import sys
import uuid

import httpx
from a2a.client import A2ACardResolver, ClientConfig, create_client
from a2a.helpers import get_data_parts, get_message_text, new_data_part
from a2a.types import Message, Role, SendMessageRequest, TaskState

URL = "http://127.0.0.1:8010"


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


async def main(op: str, args: list[str]):
    request = {"contract": "discovery/1", "operation": op, "request_id": f"manual-{uuid.uuid4().hex[:8]}",
               "params": params(op, args)}
    async with httpx.AsyncClient(timeout=120) as http:
        card = await A2ACardResolver(httpx_client=http, base_url=URL).get_agent_card()
        client = await create_client(agent=card, client_config=ClientConfig(streaming=False, httpx_client=http))
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
        print(f"artifact {a.name}:")
        print(json.dumps(get_data_parts(a.parts)[0], indent=2, ensure_ascii=False))


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    asyncio.run(main(sys.argv[1], sys.argv[2:]))
