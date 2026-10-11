"""Metadata-only observability regression checks (no network or model calls)."""
import os
from types import SimpleNamespace

from app import telemetry


def _span(name="qa.worker.attempt", scope="rag.agent", attrs=None, events=()):
    return SimpleNamespace(
        name=name,
        instrumentation_scope=SimpleNamespace(name=scope),
        attributes=attrs or {"rag.export": True, "rag.hit_count": 3},
        events=events,
        status=SimpleNamespace(description=None),
    )


def test_disabled_without_explicit_switch(monkeypatch):
    monkeypatch.delenv("RAG_LANGFUSE_ENABLED", raising=False)
    assert telemetry._enabled() is False


def test_hmac_is_versioned_and_scoped(monkeypatch):
    monkeypatch.setenv("RAG_LANGFUSE_ID_HMAC_KEY", "x" * 40)
    monkeypatch.setenv("RAG_LANGFUSE_HMAC_VERSION", "v1")
    monkeypatch.setenv("RAG_ENVIRONMENT", "test")
    a = telemetry.identifier("run", "secret-run-id")
    b = telemetry.identifier("tenant", "secret-run-id")
    assert a and len(a) == 32 and a != b and "secret" not in a
    monkeypatch.setenv("RAG_LANGFUSE_HMAC_VERSION", "v2")
    assert telemetry.identifier("run", "secret-run-id") != a
    monkeypatch.delenv("RAG_LANGFUSE_ID_HMAC_KEY", raising=False)
    assert telemetry.identifier("run", "secret-run-id") is None


def test_metadata_allowlist_fails_closed():
    assert telemetry.safe_attributes({
        "rag.hit_count": 2,
        "rag.error_code": "BUDGET_EXCEEDED",
        "rag.call_hmac": "a" * 32,
        "rag.job_hmac": "source-document-id",
        "tool.call_id": "raw-tool-uuid",
        "exception.message": "secret stack",
        "rag.model_alias": "evil key with spaces!",
    }) == {"rag.hit_count": 2, "rag.error_code": "BUDGET_EXCEEDED",
           "rag.call_hmac": "a" * 32}


def test_scope_and_exception_events_fail_closed():
    assert telemetry._export_span(_span())
    assert not telemetry._export_span(_span(scope="sqlalchemy"))
    assert not telemetry._export_span(_span(attrs={
        "rag.export": True, "gen_ai.prompt": "confidential contents",
    }))
    assert not telemetry._export_span(_span(events=(
        SimpleNamespace(name="exception", attributes={"exception.message": "secret"}),
    )))
    assert not telemetry._export_span(_span(events=(
        SimpleNamespace(name="guardrail.indirect_prompt_injection",
                        attributes={"evidence.id": "E999"}),
    )))


def test_independent_batches_are_context_local():
    assert telemetry.active_batch() is None
    with telemetry.observation_batch({"kind": "INGEST"}):
        assert telemetry.active_batch() is None
    assert telemetry.active_batch() is None
