from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional

try:
    from opentelemetry import trace
    _tracer = trace.get_tracer("rag.agent")
except Exception:
    trace = None
    _tracer = None


@contextmanager
def agent_span(name: str, attributes: Optional[Dict[str, Any]] = None) -> Iterator[Any]:
    if _tracer is None:
        yield None
        return
    with _tracer.start_as_current_span(name) as span:
        for key, value in (attributes or {}).items():
            if value is not None:
                span.set_attribute(key, value)
        try:
            yield span
        except Exception as exc:
            span.record_exception(exc)
            raise


def add_span_event(name: str, attributes: Optional[Dict[str, Any]] = None) -> None:
    if trace is None:
        return
    span = trace.get_current_span()
    if span:
        span.add_event(name, attributes=attributes or {})
