import hashlib
import math
import os
import logging
from typing import Iterable, List

from app.config import settings, is_test_env
from app.errors import ApiException
from app.qa.model_clients import get_http_client

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
            resp = client.post(
                endpoint,
                headers={"Authorization": f"Bearer {master_key}"},
                json={"model": "embedding-main", "input": batch, "dimensions": dim},
                timeout=30.0,
            )
            if resp.status_code != 200:
                err_msg = f"大模型嵌入接口返回异常状态码 {resp.status_code}: {resp.text}"
                logger.error(err_msg)
                if not is_test_env():
                    raise ApiException(503, "MODEL_GATEWAY_UNAVAILABLE", err_msg)
                return [generate_deterministic_embedding(t, dim) for t in texts]

            data = resp.json().get("data", [])
            if not isinstance(data, list) or len(data) != len(batch):
                raise ApiException(502, "INVALID_MODEL_OUTPUT", "Embedding 返回数量与输入批次不一致")

            rows = []
            seen_indexes = set()
            for item in data:
                if not isinstance(item, dict):
                    raise ApiException(502, "INVALID_MODEL_OUTPUT", "Embedding 返回项格式无效")
                index = item.get("index")
                if not isinstance(index, int) or index < 0 or index >= len(batch) or index in seen_indexes:
                    raise ApiException(502, "INVALID_MODEL_OUTPUT", "Embedding 返回 index 缺失、越界或重复")
                seen_indexes.add(index)
                rows.append((index, item.get("embedding")))

            rows.sort(key=lambda item: item[0])
            batch_vectors = []
            for expected_index, (actual_index, vector) in enumerate(rows):
                if actual_index != expected_index:
                    raise ApiException(502, "INVALID_MODEL_OUTPUT", "Embedding 返回 index 不连续")
                if not isinstance(vector, list) or len(vector) != dim:
                    actual = len(vector) if isinstance(vector, list) else 0
                    raise ApiException(
                        502,
                        "INVALID_MODEL_OUTPUT",
                        f"模型生成的嵌入向量维度不符 (预期 {dim}, 得到 {actual})",
                    )
                try:
                    numeric = [float(value) for value in vector]
                except (TypeError, ValueError) as exc:
                    raise ApiException(502, "INVALID_MODEL_OUTPUT", "Embedding 包含非数值元素") from exc
                if not all(math.isfinite(value) for value in numeric) or not any(value != 0.0 for value in numeric):
                    raise ApiException(502, "INVALID_MODEL_OUTPUT", "Embedding 包含 NaN/Inf 或零向量")
                batch_vectors.append(numeric)

            vectors.extend(batch_vectors)

        return vectors
    except ApiException:
        raise
    except Exception as exc:
        logger.error("大模型嵌入网关连接失败: %s", exc)
        if not is_test_env():
            raise ApiException(503, "MODEL_GATEWAY_UNAVAILABLE", f"大模型嵌入网关连接失败: {exc}") from exc
        return [generate_deterministic_embedding(t, dim) for t in texts]
