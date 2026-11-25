"""
The discovery/1 contract envelope (docs: Streamwise Concierge project, phase0-a2a-contract.md).

Phase 1 checks the ENVELOPE only: contract, operation, request_id, params is an object. Each operation's
params and its result data are validated in Phase 2, when the operations are implemented.

Values arrive from an A2A data part, i.e. a protobuf Struct: every number is a float (3 -> 3.0).
"""
import re
from dataclasses import dataclass

CONTRACT = "discovery/1"

# operation -> (needs a model?, one-line description for the Agent Card)
OPERATIONS = {
    "lookup_title": (False, "Where one movie or series streams in India (Netflix, Prime Video), with content ids."),
    "lookup_titles": (False, "Check up to 12 titles at once; per-title found / not found / error."),
    "search_youtube": (False, "Find a song or video on YouTube; returns video ids."),
    "discover": (True, "Answer a free-text streaming discovery question; results come only from real searches."),
}

REQUEST_ID = re.compile(r"^[A-Za-z0-9#:._-]{1,100}$")


class ContractError(Exception):
    """code: UNSUPPORTED_CONTRACT | INVALID_INPUT. message: safe to send back (no internals)."""

    def __init__(self, code: str, message: str, request: dict | None = None):
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.request = code, message, request or {}


@dataclass(frozen=True)
class Request:
    operation: str
    request_id: str
    params: dict


def parse_request(data) -> Request:
    if not isinstance(data, dict):
        raise ContractError("INVALID_INPUT", "the message must carry one JSON object (a data part)")
    if data.get("contract") != CONTRACT:
        raise ContractError("UNSUPPORTED_CONTRACT",
                            f"contract {data.get('contract')!r} is not supported; this agent speaks {CONTRACT}")
    echo = {k: data.get(k) for k in ("operation", "request_id") if isinstance(data.get(k), str)}
    request_id = data.get("request_id")
    if not isinstance(request_id, str) or not REQUEST_ID.match(request_id):
        raise ContractError("INVALID_INPUT", "request_id must be 1-100 characters of A-Z a-z 0-9 # : . _ -", echo)
    operation = data.get("operation")
    if operation not in OPERATIONS:
        raise ContractError("INVALID_INPUT", f"unknown operation {operation!r}; known: {sorted(OPERATIONS)}", echo)
    params = data.get("params")
    if not isinstance(params, dict):
        raise ContractError("INVALID_INPUT", "params must be an object", echo)
    return Request(operation, request_id, params)


def envelope(operation: str | None, request_id: str | None, outcome: str, data: dict | None = None,
             error: dict | None = None) -> dict:
    """The result artifact's JSON (contract §4)."""
    return {"contract": CONTRACT, "operation": operation, "request_id": request_id, "outcome": outcome,
            "error": error, "data": data}


def error(code: str, message: str, retry_after_s: int | None = None) -> dict:
    return {"code": code, "message": message[:300], "retry_after_s": retry_after_s}
