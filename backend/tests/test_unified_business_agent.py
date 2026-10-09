"""统一 Agent 模拟工具回归；绝不连接 Java 或真实用户。"""
import json
import pytest

from app.errors import ApiException
from app.qa.business_tools import (
    BUSINESS_TOOLS, MOCK_NOTICE, business_evidence_text, execute_business_tool,
)
from app.qa.model_gateway import ModelGateway


@pytest.mark.parametrize("name,args,condition", [
    ("diagnose_coupon", {"user_id": "u1", "coupon_id": "c1"}, "ELIGIBLE_AMOUNT_BELOW_THRESHOLD"),
    ("diagnose_product_group", {"user_id": "u1", "product_group_id": "pg1"}, "USER_REGION_NOT_TARGETED"),
    ("diagnose_subsidy", {"user_id": "u1", "activity_id": "act1"}, "NOT_NEW_CUSTOMER"),
])
def test_fixed_mock_diagnosis(name, args, condition):
    result = execute_business_tool(name, args)
    assert result["status"] == "OK" and result["mock"] is True
    assert result["data"]["failedCondition"] == condition
    assert result["queryObjects"] == args
    assert MOCK_NOTICE in business_evidence_text(name, result)


def test_price_rules_and_schema_are_no_network():
    result = execute_business_tool("get_final_price_rules", {})
    assert result["data"]["exampleFen"]["final"] == 14000
    assert "200-20-30-10=140" in business_evidence_text("get_final_price_rules", result)
    assert {item["function"]["name"] for item in BUSINESS_TOOLS} == {
        "diagnose_coupon", "diagnose_product_group", "diagnose_subsidy", "get_final_price_rules"
    }


@pytest.mark.parametrize("name,args", [
    ("diagnose_coupon", {"coupon_id": "c1"}),
    ("diagnose_coupon", {"user_id": "u1", "coupon_id": "c1", "extra": "x"}),
    ("diagnose_subsidy", {"user_id": "a b", "activity_id": "a1"}),
    ("get_final_price_rules", {"user_id": "fake"}),
    ("nonexistent", {}),
])
def test_invalid_parameters_fail_closed(name, args):
    with pytest.raises(ApiException) as exc:
        execute_business_tool(name, args)
    assert exc.value.code in {"INVALID_TOOL_ARGUMENTS", "UNKNOWN_TOOL"}


def test_unified_legacy_modes_execute_mock_tool_and_persist_evidence(client):
    csrf = client.get("/api/v1/auth/csrf").json()["csrfToken"]
    login = client.post("/api/v1/auth/login", json={
        "tenantCode": "demo", "loginName": "admin", "password": "admin123"
    }, headers={"X-CSRF-Token": csrf})
    assert login.status_code == 200
    csrf = client.get("/api/v1/auth/csrf").json()["csrfToken"]

    def handler(messages):
        if any(m.get("role") == "tool" for m in messages):
            item = json.loads(next(m["content"] for m in messages if m.get("role") == "tool"))
            assert item["mock"] is True and item["evidence"][0]["sourceType"] == "TOOL"
            return {"role": "assistant", "content": json.dumps({
                "status": "COMPLETED", "claims": [{"id": "c1", "text": "仅为模拟业务判断",
                    "evidence": [{"evidenceId": "E1", "start": 0, "end": 10}]}],
                "gaps": [], "conflicts": []}), "tool_calls": []}
        return {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call-coupon", "name": "diagnose_coupon",
             "args": {"user_id": "u1", "coupon_id": "c1"}}]}

    ModelGateway._agent_mock_handler = handler
    try:
        for i, mode in enumerate([None, "RAG", "AGENT"]):
            c = client.post("/api/v1/conversations", json={"title": "模拟诊断"},
                            headers={"X-CSRF-Token": csrf}).json()
            body = {"question": "u1 为什么不能用 c1 优惠券？", "segmentId": c["currentSegmentId"]}
            if mode:
                body["mode"] = mode
            r = client.post(f"/api/v1/conversations/{c['conversationId']}/runs",
                            json=body, headers={"X-CSRF-Token": csrf, "Idempotency-Key": f"ua-{i}"})
            assert r.status_code == 202, r.text
            assert r.json()["executionMode"] == "DURABLE"
            d = client.get("/api/v1/runs/" + r.json()["runId"]).json()
            assert d["status"] in {"COMPLETED", "SUCCEEDED"}, d
            assert d["mock"] is True and d["evidence"][0]["sourceType"] == "TOOL"
            e = client.get(f"/api/v1/runs/{r.json()['runId']}/evidence/E1")
            assert e.status_code == 200
            assert e.json()["queryObjects"] == {"user_id": "u1", "coupon_id": "c1"}
    finally:
        ModelGateway._agent_mock_handler = None
