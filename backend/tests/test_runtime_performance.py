import os

from app.qa.agent import LangGraphAgent
from app.retrieval import embedding


def test_agent_graph_is_compiled_once():
    LangGraphAgent._graph = None
    first = LangGraphAgent._get_graph()
    second = LangGraphAgent._get_graph()
    assert first is second


def test_embedding_batches_large_requests(monkeypatch):
    monkeypatch.setenv("LITELLM_BASE_URL", "http://gateway/v1")
    monkeypatch.setenv("EMBEDDING_BATCH_SIZE", "2")

    calls = []

    class Response:
        status_code = 200
        text = ""
        def __init__(self, batch):
            self.batch = batch
        def json(self):
            return {
                "data": [
                    {"embedding": [float(i + 1)] * 4}
                    for i, _ in enumerate(self.batch)
                ]
            }

    class Client:
        def post(self, url, headers=None, json=None, timeout=None):
            calls.append(list(json["input"]))
            return Response(json["input"])

    monkeypatch.setattr(embedding, "get_http_client", lambda: Client())
    result = embedding.get_embeddings(["a", "b", "c", "d", "e"], dim=4)
    assert len(result) == 5
    assert [len(batch) for batch in calls] == [2, 2, 1]
