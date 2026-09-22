import json
from datetime import datetime, timezone

import pytest
import importlib.util
from pathlib import Path

from app.db import get_db_connection
from app.evaluation.judge import parse_verdicts
from app.evaluation.runner import EvaluationRunner
from app.identity.deps import Actor
from app.models import eval_result_table


@pytest.mark.parametrize("fact,claim,label", [
    ("COVERED", "SUPPORTED", "CORRECT"),
    ("COVERED", "UNSUPPORTED", "WRONG"),
    ("CONTRADICTED", "SUPPORTED", "WRONG"),
    ("MISSING", "SUPPORTED", "WRONG"),
    ("COVERED", "UNCERTAIN", "UNDETERMINED"),
])
def test_semantic_scoring_requires_facts_and_support(fact, claim, label):
    raw = json.dumps({"facts": [{"index": 0, "verdict": fact}],
                      "claims": [{"index": 0, "verdict": claim}]})
    assert parse_verdicts(raw, ["gold"], ["answer"])["correctness"] == label


def test_incomplete_or_duplicate_judgments_fail_closed():
    with pytest.raises(ValueError):
        parse_verdicts('{"facts":[],"claims":[]}', ["gold"], ["answer"])
    with pytest.raises(ValueError):
        parse_verdicts(json.dumps({"facts": [{"index": 0, "verdict": "COVERED"}] * 2,
                                  "claims": []}), ["a", "b"], [])


def test_review_changes_summary_without_erasing_automatic_result():
    actor = Actor(tenant_id="tenant-demo-1", user_id="user-admin-1", admin=True)
    now = datetime.now(timezone.utc)
    with get_db_connection() as conn:
        for i in range(2):
            conn.execute(eval_result_table.insert().values(
                tenant_id=actor.tenant_id, id=f"result-{i}", eval_run_id="run-quality",
                dataset_version_id="dataset", case_id=f"case-{i}", repeat_no=0,
                status="COMPLETED", metrics_json={"taskSuccess": None, "costCny": None},
                created_at=now, updated_at=now,
            ))
        conn.commit()
    before = EvaluationRunner.aggregate(actor, "run-quality")
    assert before["taskSuccessRate"] is None
    assert before["totalCostCny"] is None
    EvaluationRunner.review(actor, "result-0", {"labels": {"correctness": "CORRECT"}, "reason": "verified"})
    after = EvaluationRunner.aggregate(actor, "run-quality")
    assert after["taskSuccessRate"] == 1.0
    assert after["judgmentCoverage"] == 0.5
    assert after["unknownCostCases"] == 2
    EvaluationRunner.review(actor, "result-0", {"labels": {"correctness": "WRONG"}, "reason": "corrected"})
    assert EvaluationRunner.aggregate(actor, "run-quality")["taskSuccessRate"] == 0.0


def test_compare_rejects_different_modes(monkeypatch):
    actor = Actor(tenant_id="tenant-demo-1", user_id="user-admin-1", admin=True)
    monkeypatch.setattr(EvaluationRunner, "get_run", lambda actor, rid: {"manifest": {
        "datasetVersionId": "d", "snapshotId": "s", "split": "TEST", "mode": rid,
    }})
    monkeypatch.setattr(EvaluationRunner, "results", lambda *args: [])
    comparison = EvaluationRunner.compare(actor, ["RAG", "AGENT"])
    assert comparison["comparable"] is False
    assert comparison["differingFields"] == ["mode"]


def test_matching_quote_from_wrong_version_or_unknown_document_is_not_a_hit():
    actor = Actor(tenant_id="tenant-demo-1", user_id="user-admin-1", admin=True)
    case = {"expected": {"evidenceGroups": [{"fact": "f", "alternatives": [{
        "documentId": "doc", "versionId": "v1", "quote": "known fact", "relevanceGrade": 3,
    }]}]}}
    for hit in [{"documentId": "doc", "versionId": "v2", "text": "known fact"},
                {"versionId": "v1", "text": "known fact"}]:
        assert EvaluationRunner._compute_retrieval_metrics(actor, case, [hit], [hit])["hit"] == 0
    hit = {"documentId": "doc", "versionId": "v1", "text": "known fact"}
    assert EvaluationRunner._compute_retrieval_metrics(actor, case, [hit], [hit])["hit"] == 1


def test_release_gate_rejects_unknowns_and_retrieval_only():
    spec = importlib.util.spec_from_file_location("gate", Path(__file__).parents[2] / "scripts/check_evaluation_gate.py")
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    report = {"manifest": {"mode": "RAG"}, "metrics": {
        "planned": 10, "completed": 10, "judgmentCoverage": 1,
        "taskSuccessRate": 0.9, "totalCostCny": 1.5,
    }}
    assert gate.failures(report, 0.9, 2) == []
    report["metrics"]["totalCostCny"] = None
    assert gate.failures(report, 0.9, 2)
    report["metrics"]["totalCostCny"] = 1.5
    report["manifest"]["mode"] = "RETRIEVAL_ONLY"
    assert gate.failures(report, 0.9, 2)
