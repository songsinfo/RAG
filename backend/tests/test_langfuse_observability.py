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


def test_sdk_json_metadata_and_usage_must_be_allowlisted():
    import json
    valid = {
        "langfuse.observation.type": "generation",
        "langfuse.observation.model.name": "chat-main",
        "langfuse.observation.metadata": json.dumps({
            "purpose": "AGENT", "call": "b"*32, "durationMs": 10.5}),
        "langfuse.observation.usage_details": json.dumps({
            "input": 31, "output": 0}),
    }
    assert telemetry._export_span(_span(name="model.agent", scope="langfuse-sdk", attrs=valid))
    for key, value in [
        ("langfuse.observation.metadata", '{"prompt":"TOP_SECRET"}'),
        ("langfuse.observation.usage_details", '{"input":10,"raw_input":"SECRET"}'),
        ("langfuse.observation.input", "SENSITIVE_PROMPT"),
    ]:
        unsafe = dict(valid, **{key: value})
        assert not telemetry._export_span(_span(name="model.agent", scope="langfuse-sdk", attrs=unsafe))


def test_candidate_snapshot_is_ordered_and_marks_truncation():
    from app.evaluation.diagnostics import candidate_snapshot
    candidates = [
        {"chunkId": "a", "buildId": "b1", "documentId": "d1",
         "sourceUnits": [{"unitId": "u1", "contextStart": 0, "contextEnd": 10,
                          "contextMapped": True, "location": {"page": 2}}]},
        {"chunkId": "c", "buildId": "b2", "documentId": "d2", "sourceUnits": []},
    ]
    snap = candidate_snapshot("rerank", candidates, limit=1, retrieval_index=7)
    assert snap["complete"] is False
    assert snap["capturedCount"] == 1
    assert snap["items"][0]["rank"] == 1
    assert snap["items"][0]["chunkId"] == "a"
    assert "text" not in str(snap)
    assert candidate_snapshot("context_budget", candidates[:1], limit=1,
                              retrieval_index=7, context=True)["complete"]


def test_score_projection_retains_unknown_and_human_revisions():
    from app.evaluation.langfuse_sync import _create_scores, score_id
    item = {
        "automaticJudgment": {"correctness": "UNDETERMINED",
                              "answerRelevance": "RELEVANT"},
        "metrics": {"hit": 0.0, "recall": None, "ndcg": 0.7,
                    "costCny": None},
    }
    reviews = [{"id": "r1", "labels": {"correctness": "CORRECT"}},
               {"id": "r2", "labels": {"correctness": "WRONG"}}]
    scores = list(_create_scores(item, reviews, "a"*32, "project", "v2"))
    assert any(s["value"] == "UNDETERMINED" for s in scores)
    assert len([s for s in scores if s["name"] == "human.correctness.v2"]) == 2
    assert not any(s["name"].endswith("recall.v1") for s in scores)
    assert any(s["value"] == 0.0 and s["name"] == "automatic.hit.v1" for s in scores)
    assert score_id("p", "r", "correctness", "v2", "r1") == score_id(
        "p", "r", "correctness", "v2", "r1")
    assert score_id("p", "r", "correctness", "v2", "r1") != score_id(
        "p", "r", "correctness", "v2", "r2")


def test_actual_sdk_generation_span_is_safe_and_parented(monkeypatch):
    """Inspect SDK-generated OTel attributes, not only handcrafted fake spans."""
    from langfuse import Langfuse
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import InMemorySpanExporter, SimpleSpanProcessor

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    sdk = Langfuse(
        public_key="pk-lf-test-123", secret_key="sk-lf-test-123",
        base_url="http://127.0.0.1:1", tracer_provider=provider,
        tracing_enabled=True, flush_at=1000, flush_interval=600,
        should_export_span=telemetry._export_span,
    )
    with provider.get_tracer("rag.agent").start_as_current_span(
        "qa.worker.attempt", record_exception=False
    ) as root:
        root.set_attribute("rag.export", True)
        obs = sdk.start_observation(as_type="generation", name="model.answer",
                                    model="chat-main", metadata={
                                        "purpose": "ANSWER", "call": "b" * 32})
        obs.update(usage_details={"input": 12, "output": 2})
        obs.end()
    spans = exporter.get_finished_spans()
    assert len(spans) >= 2
    assert any(s.name == "model.answer" for s in spans)
    for span in spans:
        if span.name == "model.answer":
            assert span.context.trace_id == root.get_span_context().trace_id
            assert telemetry._export_span(span), dict(span.attributes or {})
            assert "confidential" not in str(span.attributes)
