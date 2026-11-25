"""
Agent B's A2A server: Agent Card, JSON-RPC endpoint, /healthz and /readyz.

    GET  /.well-known/agent-card.json   discovery: who this agent is, one skill per discovery/1 operation
    POST /                              A2A JSON-RPC: one message (a data part) -> one task
    GET  /healthz                       the process is up
    GET  /readyz                        configuration is complete (names of missing settings, never values)

Task outcomes (contract §4):
    contract not discovery/1          -> task REJECTED   (UNSUPPORTED_CONTRACT)
    envelope or params invalid        -> task COMPLETED, result outcome "invalid_input"
    lookup_title / lookup_titles /
    search_youtube (Phase 2, no model)-> task COMPLETED, result outcome ok | rate_limited | provider_error
    discover                          -> task FAILED     (NOT_IMPLEMENTED until Phase 2b)
    a result that breaks the schema   -> task FAILED     (INTERNAL_ERROR: never sent)
    unexpected crash                  -> task FAILED     (INTERNAL_ERROR; no internals in the message)

Run: python -m discovery_agent      (127.0.0.1:DISCOVERY_PORT, default 8010)
"""
import logging

from a2a.helpers import get_data_parts, new_data_part, new_task_from_user_message, new_text_message
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill, TaskState
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from discovery_agent import config, operations
from discovery_agent.contract import (CONTRACT, OPERATIONS, ContractError, envelope, error, parse_request,
                                      validate_result)

log = logging.getLogger("discovery_agent")


def agent_card(url: str) -> AgentCard:
    skills = [
        AgentSkill(id=op, name=op.replace("_", " "), description=desc,
                   tags=["discovery", f"contract:{CONTRACT}", "model" if needs_model else "no-model"],
                   input_modes=["application/json"], output_modes=["application/json"])
        for op, (needs_model, desc) in OPERATIONS.items()
    ]
    return AgentCard(
        name=config.AGENT_NAME,
        description=("Finds where movies and series stream in India (Netflix, Prime Video) and songs or videos on "
                     f"YouTube. Speaks {CONTRACT}. Cannot play anything; holds no household or personal data."),
        version=config.AGENT_VERSION,
        default_input_modes=["application/json"],
        default_output_modes=["application/json"],
        capabilities=AgentCapabilities(streaming=False),
        supported_interfaces=[AgentInterface(protocol_binding="JSONRPC", url=url, protocol_version="1.0")],
        skills=skills,
    )


class DiscoveryExecutor(AgentExecutor):
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task or new_task_from_user_message(context.message)
        if not context.current_task:
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue=event_queue, task_id=task.id, context_id=task.context_id)
        await updater.update_status(state=TaskState.TASK_STATE_WORKING)
        try:
            data = get_data_parts(context.message.parts)
            if len(data) != 1:
                raise ContractError("INVALID_INPUT", f"expected exactly one data part, got {len(data)}")
            request = parse_request(data[0])
        except ContractError as e:
            if e.code == "UNSUPPORTED_CONTRACT":
                await updater.update_status(state=TaskState.TASK_STATE_REJECTED,
                                            message=new_text_message(f"{e.code}: {e.message}"))
                return
            result = envelope(e.request.get("operation"), e.request.get("request_id"), "invalid_input",
                              error=error(e.code, e.message))
            await self._complete(updater, result)
            return
        except Exception:  # noqa: BLE001   never a hang, never internals in the reply
            log.exception("unexpected error while reading the request")
            await updater.update_status(state=TaskState.TASK_STATE_FAILED,
                                        message=new_text_message("INTERNAL_ERROR: the request could not be processed"))
            return

        handler = operations.HANDLERS.get(request.operation)
        if handler is None:
            log.info("request_id=%s operation=%s: not implemented yet", request.request_id, request.operation)
            await updater.update_status(
                state=TaskState.TASK_STATE_FAILED,
                message=new_text_message(f"NOT_IMPLEMENTED: {request.operation} arrives in Phase 2b"))
            return
        try:
            result = await handler(request)
        except Exception:  # noqa: BLE001
            log.exception("request_id=%s operation=%s: unexpected error", request.request_id, request.operation)
            await updater.update_status(state=TaskState.TASK_STATE_FAILED,
                                        message=new_text_message("INTERNAL_ERROR: the request could not be processed"))
            return
        log.info("request_id=%s operation=%s outcome=%s", request.request_id, request.operation, result["outcome"])
        await self._complete(updater, result)

    @staticmethod
    async def _complete(updater: TaskUpdater, result: dict) -> None:
        """Send the result artifact, but ONLY if it matches the schema (B2: never send a broken result)."""
        try:
            validate_result(result)
        except ValueError as e:
            log.error("request_id=%s: %s", result.get("request_id"), e)
            await updater.update_status(state=TaskState.TASK_STATE_FAILED,
                                        message=new_text_message("INTERNAL_ERROR: the result failed validation"))
            return
        await updater.add_artifact(name="result", parts=[new_data_part(result, media_type="application/json")])
        await updater.update_status(state=TaskState.TASK_STATE_COMPLETED)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError("cancel is not supported")


async def healthz(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "agent": config.AGENT_NAME, "version": config.AGENT_VERSION,
                         "contract": CONTRACT})


async def readyz(_: Request) -> JSONResponse:
    missing = config.missing_for_model() + config.missing_for_catalog()
    body = {"ready": not missing, "missing": missing}
    return JSONResponse(body, status_code=200 if not missing else 503)


def build_app(url: str) -> Starlette:
    card = agent_card(url)
    handler = DefaultRequestHandler(agent_executor=DiscoveryExecutor(), task_store=InMemoryTaskStore(),
                                    agent_card=card)
    return Starlette(routes=[*create_agent_card_routes(card), *create_jsonrpc_routes(handler, "/"),
                             Route("/healthz", healthz), Route("/readyz", readyz)])


def main():
    import uvicorn
    host = config.check_host(config.HOST)
    url = f"http://{host}:{config.PORT}"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    print(f"{config.AGENT_NAME} (A2A, {CONTRACT}) on {url}  card: {url}/.well-known/agent-card.json  "
          "(no authentication yet: this PC only)", flush=True)
    uvicorn.run(build_app(url), host=host, port=config.PORT, log_level="warning")
