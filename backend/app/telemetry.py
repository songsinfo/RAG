"""Opt-in, metadata-only Langfuse diagnostics sharing the application's OTel provider.

No content or business identifiers leave this module. MySQL remains authoritative.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
import threading
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterator, Optional

logger = logging.getLogger(__name__)
_lock = threading.RLock()
_configured = False
_provider = None
_client = None
_status = "DISABLED"
_pid = os.getpid()
_batch_var: ContextVar[Optional["ObservationBatch"]] = ContextVar("rag_observation_batch", default=None)

_HASH_RE = re.compile(r"^[0-9a-f]{32}$")
_CODE_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_.:/-]{0,63}$")
_SAFE_KEYS = frozenset({
    "rag.export", "rag.environment", "rag.hmac_version", "rag.tenant_hmac",
    "rag.run_hmac", "rag.job_hmac", "rag.conversation_hmac", "rag.segment_hmac",
    "rag.lease_epoch", "rag.job_attempt", "rag.retrieval_index", "rag.hit_count",
    "rag.duration_ms", "rag.input_count", "rag.used_bytes", "rag.max_bytes",
    "rag.status", "rag.error_code", "rag.model_alias", "rag.purpose",
    "rag.call_hmac", "rag.stage", "rag.enabled", "rag.route_count",
    "agent.round", "agent.prompt_version", "tool.name", "tool.category",
    "tool.mock",
})
_HASH_KEYS = frozenset({"rag.tenant_hmac", "rag.run_hmac", "rag.job_hmac",
                        "rag.conversation_hmac", "rag.segment_hmac", "rag.call_hmac"})
_NUMBER_KEYS = frozenset({"rag.lease_epoch", "rag.job_attempt", "rag.retrieval_index",
                          "rag.hit_count", "rag.duration_ms", "rag.input_count",
                          "rag.used_bytes", "rag.max_bytes", "rag.route_count",
                          "agent.round"})
_ALLOWED_EVENTS = frozenset({"guardrail.indirect_prompt_injection", "model.not_sent"})


@dataclass(frozen=True)
class ObservationBatch:
    trace_id: Optional[str]
    sampled: bool
    target: str
    job_attempt: int
    lease_epoch: int
    hmac_version: str


def _enabled() -> bool:
    return os.getenv("RAG_LANGFUSE_ENABLED", "false").strip().lower() in {"1", "true", "yes"}


def _safe_code(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    return value if _CODE_RE.fullmatch(value) else None


def safe_attributes(attributes: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Fail closed: permit named, typed scalar metadata, never arbitrary strings."""
    result: dict[str, Any] = {}
    for key, value in (attributes or {}).items():
        if key not in _SAFE_KEYS or value is None:
            continue
        if key in _HASH_KEYS:
            if isinstance(value, str) and _HASH_RE.fullmatch(value):
                result[key] = value
        elif key in _NUMBER_KEYS:
            if type(value) in {int, float} and 0 <= value < 1e12:
                result[key] = value
        elif key in {"rag.export", "rag.enabled", "tool.mock"}:
            if type(value) is bool:
                result[key] = value
        elif isinstance(value, str) and _safe_code(value):
            result[key] = value
    return result


def identifier(kind: str, value: Any) -> Optional[str]:
    """Independent, versioned HMAC; never fall back to unkeyed or plaintext IDs."""
    key = os.getenv("RAG_LANGFUSE_ID_HMAC_KEY", "")
    version = os.getenv("RAG_LANGFUSE_HMAC_VERSION", "v1")
    env = os.getenv("RAG_ENVIRONMENT", "demo")
    if len(key.encode("utf-8")) < 32 or not all(_safe_code(x) for x in (kind, version, env)):
        return None
    return hmac.new(key.encode("utf-8"),
                    (version + "|" + env + "|" + kind + "|" + str(value)).encode("utf-8"),
                    hashlib.sha256).hexdigest()[:32]


def _export_span(span: Any) -> bool:
    """Strict scope + attributes/events allowlist, including SDK generation spans."""
    try:
        scope = span.instrumentation_scope.name
        if scope == "langfuse-sdk":
            return (str(span.name).startswith("model.")
                    and not span.events
                    and not getattr(span.status, "description", None))
        if scope != "rag.agent" or not str(span.name).startswith(("qa.", "agent.", "rag.")):
            return False
        attrs = dict(span.attributes or {})
        if attrs.get("rag.export") is not True:
            return False
        if len(safe_attributes(attrs)) != len(attrs) or getattr(span.status, "description", None):
            return False
        for event in span.events or ():
            if event.name not in _ALLOWED_EVENTS:
                return False
            if len(safe_attributes(dict(event.attributes or {}))) != len(event.attributes or {}):
                return False
        return True
    except Exception:
        return False


def _mask_otel_spans(*, params):
    """Defense in depth; the export filter already rejects unknown events/scopes.

    Raw OTel events cannot be patched by the SDK, hence _export_span rejects them.
    """
    from langfuse.types import MaskOtelSpansResult, OtelSpanPatch

    patches = {}
    sensitive_parts = ("input", "output", "prompt", "completion", "exception",
                       "stack", "header", "body", "url", "question", "answer",
                       "document", "message", "path", "content", "reason")
    for ref, span in params.spans.items():
        to_delete = tuple(k for k in span.attributes if
                          any(s in k.lower() for s in sensitive_parts))
        if to_delete:
            patches[ref] = OtelSpanPatch(delete_attributes=to_delete)
    return MaskOtelSpansResult(span_patches=patches)


def configure_telemetry() -> None:
    """One provider per process; no duplicate processor when embedded workers start."""
    global _configured, _provider, _client, _status, _pid
    with _lock:
        if _pid != os.getpid():
            _configured, _provider, _client = False, None, None
            _pid = os.getpid()
        if _configured:
            return
        _configured = True
        endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
        lf_requested = _enabled()
        if not endpoint and not lf_requested:
            _status = "DISABLED"
            return
        try:
            from opentelemetry import trace
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            provider = trace.get_tracer_provider()
            if not isinstance(provider, TracerProvider):
                candidate = TracerProvider(resource=Resource.create({
                    "service.name": os.getenv("OTEL_SERVICE_NAME", "rag-backend")
                }))
                trace.set_tracer_provider(candidate)
                provider = trace.get_tracer_provider()
            if not isinstance(provider, TracerProvider):
                _status = "PROVIDER_UNAVAILABLE"
                return
            _provider = provider
            if endpoint:
                exporter = OTLPSpanExporter(endpoint=endpoint.rstrip("/") + "/v1/traces", timeout=2)
                provider.add_span_processor(BatchSpanProcessor(
                    exporter, max_queue_size=512, max_export_batch_size=128,
                    schedule_delay_millis=1000, export_timeout_millis=2000))
            if not lf_requested:
                _status = "OTLP_ONLY"
                return
            if not all(os.getenv(k, "").strip() for k in (
                    "LANGFUSE_BASE_URL", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")):
                _status = "MISSING_CONFIG"
                logger.warning("Langfuse disabled: missing configuration")
                return
            from langfuse import Langfuse
            # v4 attaches a single LangfuseSpanProcessor to the supplied shared provider.
            _client = Langfuse(
                public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
                secret_key=os.environ["LANGFUSE_SECRET_KEY"],
                base_url=os.environ["LANGFUSE_BASE_URL"],
                tracer_provider=provider,
                sample_rate=1.0,  # batch-level sampling is decided before spans start
                should_export_span=_export_span,
                mask_otel_spans=_mask_otel_spans,
            )
            _status = "ENABLED"
            logger.info("Langfuse metadata-only tracing enabled")
        except Exception:
            _client = None
            _status = "INIT_FAILED"
            logger.warning("Langfuse/OTel initialization failed; business execution unaffected")


def telemetry_status() -> str:
    return _status


def active_batch() -> Optional[ObservationBatch]:
    return _batch_var.get()


def export_current_span() -> bool:
    batch = active_batch()
    return bool(batch and batch.sampled and _client is not None)


@contextmanager
def observation_batch(job: dict[str, Any]) -> Iterator[Optional[ObservationBatch]]:
    """New physical OTel trace per claimed QA lease; never a span across HITL/restart."""
    if job.get("kind") != "QA_RUN" or _client is None:
        yield None
        return
    try:
        rate = float(os.getenv("RAG_LANGFUSE_SAMPLE_RATE", "1"))
        if not (0 <= rate <= 1):
            raise ValueError("bad sample rate")
    except ValueError:
        rate = 0.0  # configuration error fails closed
    sampled = secrets.randbelow(1000000) < int(rate * 1000000)
    attempt = int(job.get("attempt") or 0) + 1
    epoch = int(job.get("lease_epoch") or 0)
    target = _safe_code(os.getenv("RAG_LANGFUSE_TARGET_ID", "private")) or "private"
    version = _safe_code(os.getenv("RAG_LANGFUSE_HMAC_VERSION", "v1")) or "v1"
    from opentelemetry import trace
    tracer = trace.get_tracer("rag.agent")
    # Disabled sample still has a LOCAL mapping event; no raw IDs are exported.
    with tracer.start_as_current_span("qa.worker.attempt",
                                      record_exception=False,
                                      set_status_on_exception=False) as span:
        span.set_attribute("rag.export", sampled)
        for key, val in safe_attributes({
            "rag.job_attempt": attempt, "rag.lease_epoch": epoch,
            "rag.environment": os.getenv("RAG_ENVIRONMENT", "demo"),
            "rag.hmac_version": version,
            "rag.tenant_hmac": identifier("tenant", job.get("tenant_id")),
            "rag.run_hmac": identifier("run", job.get("target_id")),
            "rag.job_hmac": identifier("job", job.get("id")),
        }).items():
            span.set_attribute(key, val)
        trace_id = format(span.get_span_context().trace_id, "032x") if span.is_recording() else None
        batch = ObservationBatch(trace_id, bool(sampled and trace_id), target, attempt, epoch, version)
        token = _batch_var.set(batch)
        try:
            yield batch
        finally:
            _batch_var.reset(token)


@contextmanager
def qa_identity_scope(conversation_id: str, segment_id: str) -> Iterator[None]:
    """Update root + propagate HMAC session/identities to Langfuse children."""
    batch = active_batch()
    if batch is None or not batch.sampled:
        yield
        return
    from opentelemetry import trace
    attrs = safe_attributes({
        "rag.conversation_hmac": identifier("conversation", conversation_id),
        "rag.segment_hmac": identifier("segment", segment_id),
    })
    for key, val in attrs.items():
        trace.get_current_span().set_attribute(key, val)
    session_id = identifier("conversation", conversation_id)
    if session_id is None:
        yield
        return
    try:
        from langfuse import propagate_attributes
        scope = propagate_attributes(session_id=session_id)
    except Exception:
        logger.warning("Langfuse session propagation unavailable")
        scope = nullcontext()
    with scope:
        yield


@contextmanager
def model_observation(purpose: str, alias: str, call_id: str) -> Iterator[Any]:
    """One manually ended generation for each genuinely sent provider attempt."""
    if not export_current_span():
        yield None
        return
    try:
        name = "model." + (_safe_code(str(purpose).lower()) or "unknown")
        model = _safe_code(alias) or "configured"
        metadata = {"purpose": _safe_code(purpose) or "UNKNOWN"}
        call_hash = identifier("call", call_id)
        if call_hash:
            metadata["call"] = call_hash
        observation = _client.start_observation(as_type="generation", name=name,
                                                 model=model, metadata=metadata)
    except Exception:
        logger.warning("Langfuse generation start failed")
        yield None
        return
    try:
        yield observation
    finally:
        try:
            observation.end()  # no exception object or traceback handed to SDK
        except Exception:
            logger.warning("Langfuse generation end failed")


def finish_model_observation(observation: Any, *, status: str, duration_ms: float,
                             reported_usage: Optional[dict] = None,
                             settlement: Optional[dict] = None,
                             error_code: Optional[str] = None) -> None:
    if observation is None:
        return
    try:
        metadata: dict[str, Any] = {"status": _safe_code(status) or "UNKNOWN",
                                     "durationMs": round(max(0.0, duration_ms), 2)}
        if error_code:
            metadata["errorCode"] = _safe_code(error_code) or "UNKNOWN"
        if settlement:
            metadata["costCurrency"] = "CNY"
            metadata["costStatus"] = "SETTLED" if settlement.get("settledAmount") is not None else "UNKNOWN"
            if settlement.get("settledAmount") is not None:
                # CNY is not automatically converted to Langfuse's native cost currency.
                try:
                    metadata["settledCny"] = round(float(settlement["settledAmount"]), 6)
                except (ValueError, TypeError, OverflowError):
                    pass
        kwargs: dict[str, Any] = {"metadata": metadata}
        if reported_usage:
            tokens = {k: v for k, v in reported_usage.items() if
                      k in {"input", "output", "cache_read_input_tokens"} and type(v) is int and v >= 0}
            if tokens:
                kwargs["usage_details"] = tokens
        observation.update(**kwargs)
    except Exception:
        logger.warning("Langfuse generation update failed")


def shutdown_telemetry(timeout_seconds: float = 2.0) -> None:
    """Bound process exit; exporter failure must never block job completion."""
    global _client, _configured, _status
    with _lock:
        client, provider = _client, _provider
        _client = None
        _configured = False
    def close():
        try:
            if client:
                client.flush()
            if provider:
                provider.shutdown()
        except Exception:
            logger.warning("Telemetry flush/shutdown failed")
    if client or provider:
        thread = threading.Thread(target=close, name="rag-telemetry-close", daemon=True)
        thread.start()
        thread.join(timeout=max(0.0, min(float(timeout_seconds), 10.0)))
        if thread.is_alive():
            _status = "SHUTDOWN_TIMEOUT"
            logger.warning("Telemetry shutdown timed out; spans may be dropped")
        else:
            _status = "STOPPED"
