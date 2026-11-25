"""Talk to Agent B's A2A app IN THIS PROCESS (httpx ASGI transport): no port, no network."""
import asyncio

import httpx
from a2a.client import A2ACardResolver, ClientConfig, create_client
from a2a.helpers import get_data_parts, get_message_text, new_data_part, new_text_part
from a2a.types import Message, Role, SendMessageRequest

import tokens

from discovery_agent.server import build_app

URL = "http://discovery-agent.test"
DEFAULT = object()


def http(token=DEFAULT) -> httpx.AsyncClient:
    """A client for the in-process app. By default it sends a valid concierge-agent token;
    token=None sends none, any string is sent as given."""
    token = tokens.good() if token is DEFAULT else token
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(URL)), base_url=URL, headers=headers)


def send(*parts, token=DEFAULT):
    """Send one message with these parts; return the final task."""
    async def go():
        async with http(token) as h:
            card = await A2ACardResolver(httpx_client=h, base_url=URL).get_agent_card()
            client = await create_client(agent=card, client_config=ClientConfig(streaming=False, httpx_client=h))
            task = None
            try:
                msg = Message(role=Role.ROLE_USER, message_id="m-1", parts=list(parts))
                async for chunk in client.send_message(SendMessageRequest(message=msg)):
                    if chunk.HasField("task"):
                        task = chunk.task
            finally:
                await client.close()
            return task
    return asyncio.run(go())


def data(obj):
    return new_data_part(obj, media_type="application/json")


def text(s):
    return new_text_part(text=s, media_type="text/plain")


def result_of(task) -> dict:
    by_name = {a.name: a for a in task.artifacts}
    return get_data_parts(by_name["result"].parts)[0]


def status_text(task) -> str:
    return get_message_text(task.status.message) if task.status.HasField("message") else ""
