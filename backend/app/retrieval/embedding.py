import hashlib
import math
import os
import logging
from typing import List
import httpx
from app.config import settings, is_test_env
from app.errors import ApiException

logger = logging.getLogger(__name__)


def generate_deterministic_embedding(text: str, dim: int = 1024) -> List[float]:
    """Generates a deterministic 1024-dim unit vector for testing."""
    vec = []
    base_bytes = text.encode("utf-8")
    for i in range(dim):
        h = hashlib.sha256(base_bytes + str(i).encode("ascii")).digest()
        val = (int.from_bytes(h[:4], "big") / 2147483648.0) - 1.0
        vec.append(val)

    # Normalize to unit length
    norm = math.sqrt(sum(x * x for x in vec))
    if norm > 0:
        vec = [x / norm for x in vec]
    return vec


def get_embeddings(texts: List[str], dim: int = 1024) -> List[List[float]]:
    gateway_url = os.getenv("LITELLM_BASE_URL") or getattr(settings, "LITELLM_BASE_URL", "")
    if not gateway_url:
        if not is_test_env():
            raise ApiException(503, "MODEL_GATEWAY_UNAVAILABLE", "未配置大模型网关地址 LITELLM_BASE_URL，嵌入服务不可用")
        return [generate_deterministic_embedding(t, dim) for t in texts]

    master_key = os.getenv("LITELLM_MASTER_KEY", "sk-litellm-internal-master-key")

    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(
                f"{gateway_url}/embeddings",
                headers={"Authorization": f"Bearer {master_key}"},
                json={"model": "embedding-main", "input": texts},
            )
            if resp.status_code == 200:
                data = resp.json().get("data", [])
                vectors = [item["embedding"] for item in data]
                for v in vectors:
                    if len(v) != dim or not any(x != 0 for x in v):
                        raise ApiException(502, "INVALID_MODEL_OUTPUT", f"模型生成的嵌入向量维度不符 (预期 {dim}, 得到 {len(v)})")
                return vectors
            else:
                err_msg = f"大模型嵌入接口返回异常状态码 {resp.status_code}: {resp.text}"
                logger.error(err_msg)
                if not is_test_env():
                    raise ApiException(503, "MODEL_GATEWAY_UNAVAILABLE", err_msg)
    except ApiException:
        raise
    except Exception as e:
        logger.error("大模型嵌入网关连接失败: %s", e)
        if not is_test_env():
            raise ApiException(503, "MODEL_GATEWAY_UNAVAILABLE", f"大模型嵌入网关连接失败: {e}")

    # Fallback to deterministic embeddings ONLY for local test environments
    return [generate_deterministic_embedding(t, dim) for t in texts]
