"""
The discovery/1 contract envelope (docs: Streamwise Concierge project, phase0-a2a-contract.md).

The schema file schemas/discovery-1.schema.json is the source of truth. parse_request checks the envelope
and then the operation's params against it; validate_result checks every result BEFORE it is sent.

Values arrive from an A2A data part, i.e. a protobuf Struct: every number is a float (3 -> 3.0).
"""
import json
import re
from dataclasses import dataclass
from pathlib import Path

from jsonschema import Draft202012Validator

CONTRACT = "discovery/1"

# operation -> (needs a model?, one-line description for the Agent Card)
OPERATIONS = {
    "lookup_title": (False, "Where one movie or series streams in India (Netflix, Prime Video), with content ids."),
    "lookup_titles": (False, "Check up to 12 titles at once; per-title found / not found / error."),
    "search_youtube": (False, "Find a song or video on YouTube; returns video ids."),
    "discover": (True, "Answer a free-text streaming discovery question; results come only from real searches."),
}

REQUEST_ID = re.compile(r"^[A-Za-z0-9#:._-]{1,100}$")

SCHEMA_FILE = Path(__file__).resolve().parent.parent / "schemas" / "discovery-1.schema.json"
SCHEMA = json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))
Draft202012Validator.check_schema(SCHEMA)


def _validator(name: str) -> Draft202012Validator:
    return Draft202012Validator({"$ref": f"#/$defs/{name}", "$defs": SCHEMA["$defs"]})


PARAMS = {op: _validator(f"params_{op}") for op in OPERATIONS}
RESULT = _validator("result")


def first_problem(validator: Draft202012Validator, instance) -> str | None:
    """The first schema violation as one short line ('params.titles: ...'), or None."""
    errors = sorted(validator.iter_errors(instance), key=lambda e: list(e.absolute_path))
    if not errors:
        return None
    e = errors[0]
    where = ".".join(str(p) for p in e.absolute_path) or "(top level)"
    return f"{where}: {e.message}"[:300]


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
    # Echo back only values that are themselves valid: an invalid one in the reply would break the contract.
    echo = {}
    if isinstance(data.get("request_id"), str) and REQUEST_ID.match(data["request_id"]):
        echo["request_id"] = data["request_id"]
    if data.get("operation") in OPERATIONS:
        echo["operation"] = data["operation"]
    request_id = data.get("request_id")
    if not isinstance(request_id, str) or not REQUEST_ID.match(request_id):
        raise ContractError("INVALID_INPUT", "request_id must be 1-100 characters of A-Z a-z 0-9 # : . _ -", echo)
    operation = data.get("operation")
    if operation not in OPERATIONS:
        raise ContractError("INVALID_INPUT", f"unknown operation {operation!r}; known: {sorted(OPERATIONS)}", echo)
    params = data.get("params")
    if not isinstance(params, dict):
        raise ContractError("INVALID_INPUT", "params must be an object", echo)
    problem = first_problem(PARAMS[operation], params)
    if problem:
        raise ContractError("INVALID_INPUT", f"params.{problem}" if not problem.startswith("(") else problem, echo)
    return Request(operation, request_id, params)


def validate_result(result: dict) -> None:
    """Raise ValueError if a result Agent B is about to send breaks the contract (never send it then)."""
    problem = first_problem(RESULT, result)
    if problem:
        raise ValueError(f"result breaks {CONTRACT}: {problem}")


def envelope(operation: str | None, request_id: str | None, outcome: str, data: dict | None = None,
             error: dict | None = None, source: dict | None = None) -> dict:
    """The result artifact's JSON (contract §4). `source` is required when outcome is ok."""
    result = {"contract": CONTRACT, "operation": operation, "request_id": request_id, "outcome": outcome,
              "error": error, "data": data}
    if source is not None:
        result["source"] = source
    return result


def error(code: str, message: str, retry_after_s: int | None = None) -> dict:
    return {"code": code, "message": message[:300], "retry_after_s": retry_after_s}
