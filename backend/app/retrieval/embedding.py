import hashlib
import math
import os
import logging
from typing import Iterable, List

from app.config import settings, is_test_env
from app.errors import ApiException
from app.qa.model_clients import get_http_client
from app.qa.model_usage import invoke_tracked

logger = logging.getLogger(__name__)


def generate_deterministic_embedding(text: str, dim: int = 1024) -> List[float]:
    """Generates a deterministic unit vector for testing."""
    vec = []
    base_bytes = text.encode("utf-8")
    for i in range(dim):
        h = hashlib.sha256(base_bytes + str(i).encode("ascii")).digest()
        val = (int.from_bytes(h[:4], "big") / 2147483648.0) - 1.0
        vec.append(val)
    norm = math.sqrt(sum(x * x for x in vec))
    if norm > 0:
        vec = [x / norm for x in vec]
    return vec


def _batched(values: List[str], size: int) -> Iterable[List[str]]:
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _embedding_endpoint() -> str:
    gateway_url = (os.getenv("LITELLM_BASE_URL") or getattr(settings, "LITELLM_BASE_URL", "")).rstrip("/")
    if not gateway_url:
        return ""
    return f"{gateway_url}/embeddings" if gateway_url.endswith("/v1") else f"{gateway_url}/v1/embeddings"


def get_embeddings(
    texts: List[str],
    dim: int = 1024,
    batch_size: int | None = None,
    tenant_id: str | None = None,
) -> List[List[float]]:
    if not texts:
        return []

    endpoint = _embedding_endpoint()
    if not endpoint:
        if not is_test_env():
            raise ApiException(503, "MODEL_GATEWAY_UNAVAILABLE", "未配置大模型网关地址 LITELLM_BASE_URL，嵌入服务不可用")
        return [generate_deterministic_embedding(t, dim) for t in texts]

    master_key = os.getenv("LITELLM_MASTER_KEY", "sk-litellm-internal-master-key")
    batch_size = batch_size or int(os.getenv("EMBEDDING_BATCH_SIZE", "128"))
    batch_size = max(1, min(512, batch_size))
    vectors: List[List[float]] = []

    try:
        client = get_http_client()
        for batch in _batched(texts, batch_size):
            resp = invoke_tracked(lambda: client.post(
                endpoint,
                headers={"Authorization": f"Bearer {master_key}"},
                json={"model": "embedding-main", "input": batch},
                timeout=30.0,
            ), tenant_id=tenant_id, purpose="EMBEDDING", alias="embedding-main", payload=batch,
                usage_reader=lambda response: {"input_tokens": response.json()["usage"]["total_tokens"], "output_tokens": 0}
                if response.status_code == 200 and "total_tokens" in response.json().get("usage", {}) else {})
            if resp.status_code != 200:
                err_msg = f"大模型嵌入接口返回异常状态码 {resp.status_code}: {resp.text}"
                logger.error(err_msg)
                if not is_test_env():
                    raise ApiException(503, "MODEL_GATEWAY_UNAVAILABLE", err_msg)
                return [generate_deterministic_embedding(t, dim) for t in texts]

            data = resp.json().get("data", [])
            batch_vectors = [item.get("embedding") for item in data]
            if len(batch_vectors) != len(batch):
                raise ApiException(502, "INVALID_MODEL_OUTPUT", "Embedding 返回数量与输入批次不一致")

            for vector in batch_vectors:
                if not isinstance(vector, list) or len(vector) != dim or not any(x != 0 for x in vector):
                    actual = len(vector) if isinstance(vector, list) else 0
                    raise ApiException(
                        502,
                        "INVALID_MODEL_OUTPUT",
                        f"模型生成的嵌入向量维度不符 (预期 {dim}, 得到 {actual})",
                    )
            vectors.extend(batch_vectors)

        return vectors
    except ApiException:
        raise
    except Exception as exc:
        logger.error("大模型嵌入网关连接失败: %s", exc)
        if not is_test_env():
            raise ApiException(503, "MODEL_GATEWAY_UNAVAILABLE", f"大模型嵌入网关连接失败: {exc}") from exc
        return [generate_deterministic_embedding(t, dim) for t in texts]
