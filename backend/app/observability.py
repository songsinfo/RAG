"""Only whitelisted scalar span metadata may be exported to Langfuse."""
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional

from app.telemetry import export_current_span, safe_attributes


@contextmanager
def agent_span(name: str, attributes: Optional[Dict[str, Any]] = None) -> Iterator[Any]:
    try:
        from opentelemetry import trace
        tracer = trace.get_tracer("rag.agent")
        # No exception recording: OTel's default auto-exception events leak exception text.
        with tracer.start_as_current_span(name, record_exception=False,
                                          set_status_on_exception=False) as span:
            for key, val in safe_attributes(attributes).items():
                span.set_attribute(key, val)
            span.set_attribute("rag.export", export_current_span())
            yield span
    except ImportError:
        yield None


def add_span_event(name: str, attributes: Optional[Dict[str, Any]] = None) -> None:
    if name not in {"guardrail.indirect_prompt_injection", "model.not_sent"}:
        return
    try:
        from opentelemetry import trace
        span = trace.get_current_span()
        if span is not None and span.is_recording():
            span.add_event(name, attributes=safe_attributes(attributes))
    except Exception:
        # Span/event failures are never business failures.
        return
