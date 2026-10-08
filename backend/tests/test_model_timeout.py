import time
from types import SimpleNamespace

import httpx
import pytest

from app.qa.model_gateway import ModelGateway


@pytest.mark.parametrize("remaining", [None, 10])
def test_answer_waits_for_slow_model_without_exceeding_agent_deadline(monkeypatch, remaining):
    import app.qa.model_gateway as gateway

    monkeypatch.setenv("LITELLM_BASE_URL", "http://model.test")
    monkeypatch.setenv("AGENT_CALL_MAX_ATTEMPTS", "1")
    deadline = time.time() + remaining if remaining is not None else None

    def fake_model(alias, max_tokens, timeout_seconds):
        def invoke(messages):
            if remaining is None and timeout_seconds < 40:
                raise httpx.ReadTimeout("A valid answer needs 40 seconds")
            if deadline is not None:
                assert 0 < timeout_seconds <= remaining
            return SimpleNamespace(content="回答", usage_metadata={"input_tokens": 10, "output_tokens": 2})
        model = SimpleNamespace(invoke=invoke)
        model.bind = lambda **kwargs: model
        return model

    monkeypatch.setattr(gateway, "get_chat_model", fake_model)
    monkeypatch.setattr(gateway, "invoke_tracked", lambda fn, **kwargs: fn())
    assert ModelGateway.chat(
        "tenant-demo-1", None, "ANSWER", [{"role": "user", "content": "问题"}],
        deadline_epoch=deadline,
    ) == "回答"


@pytest.mark.parametrize("purpose", ["ANSWER", "HYDE"])
def test_answer_json_mode_preserves_plain_text_retrieval_calls(monkeypatch, purpose):
    import json
    import app.qa.model_gateway as gateway

    monkeypatch.setenv("LITELLM_BASE_URL", "http://model.test")
    monkeypatch.setattr(gateway, "invoke_tracked", lambda fn, **kwargs: fn())
    options = {}

    def invoke(messages):
        content = '{"claims":[]}' if options.get("response_format") == {"type": "json_object"} else ('```json\n{"claims":[]}\n```' if purpose == "ANSWER" else "hypothetical passage")
        return SimpleNamespace(content=content)

    model = SimpleNamespace(invoke=invoke)
    def bind(**kwargs):
        options.update(kwargs)
        return model
    model.bind = bind
    monkeypatch.setattr(gateway, "get_chat_model", lambda *args: model)
    result = ModelGateway.chat("tenant-demo-1", None, purpose, [{"role": "user", "content": "JSON 问题"}])
    if purpose == "ANSWER":
        assert json.loads(result) == {"claims": []}
    else:
        assert result == "hypothetical passage"
        assert not options
