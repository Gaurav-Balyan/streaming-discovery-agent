"""
Span helpers for Agent B. OpenTelemetry API only: until Phase 7 configures an exporter, spans are no-ops.
(Agent A's common/tracing.py also sets up exporters and the printed tree; Agent B does not need those yet.)
"""
import json
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

tracer = trace.get_tracer("discovery_agent")


@contextmanager
def span(name: str, attrs: dict | None = None):
    with tracer.start_as_current_span(name) as s:
        set_attrs(s, attrs or {})
        yield s


def set_attrs(s, attrs: dict):
    for key, value in attrs.items():
        if value is None:
            continue
        if isinstance(value, (dict, list, tuple)) and not (
                isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value)):
            value = json.dumps(value, sort_keys=True)[:500]
        s.set_attribute(key, value)


def failed(s, description: str):
    s.set_status(Status(StatusCode.ERROR, description[:200]))
