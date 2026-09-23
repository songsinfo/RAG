import hashlib
import math
import os
import logging
import uuid
from typing import Iterable, List

from app.config import settings, is_test_env
from app.errors import ApiException
from app.qa.model_clients import get_http_client
from app.model_budget import BudgetService

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
    run_id: str | None = None,
    job_id: str | None = None,
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
            call_id = str(uuid.uuid4())
            sent = False
            recorded = False
            input_tokens = max(1, sum(max(1, len(value) // 4) for value in batch))
            BudgetService.reserve(
                tenant_id=tenant_id or os.getenv("RAG_DEFAULT_TENANT_ID", "tenant-demo-1"),
                call_id=call_id,
                model_alias="embedding-main",
                input_upper_bound=input_tokens,
                output_upper_bound=0,
                run_id=run_id,
            )
            try:
                from app.qa.model_gateway import ModelGateway
                effective_tenant = tenant_id or os.getenv("RAG_DEFAULT_TENANT_ID", "tenant-demo-1")
                ModelGateway.assert_run_sources_current(effective_tenant, run_id)
                ModelGateway.assert_job_current(effective_tenant, job_id)
                sent = True
                resp = client.post(
                    endpoint,
                    headers={"Authorization": f"Bearer {master_key}"},
                    json={"model": "embedding-main", "input": batch, "dimensions": dim},
                    timeout=30.0,
                )
                if resp.status_code != 200:
                    raise ApiException(
                        503,
                        "MODEL_GATEWAY_UNAVAILABLE",
                        f"大模型嵌入接口返回异常状态码 {resp.status_code}: {resp.text}",
                    )
                data = resp.json().get("data", [])
            except Exception:
                from app.qa.model_gateway import ModelGateway
                ModelGateway._record_call(
                    tenant_id=tenant_id or os.getenv("RAG_DEFAULT_TENANT_ID", "tenant-demo-1"),
                    call_id=call_id,
                    run_id=run_id,
                    purpose="EMBEDDING",
                    status="FAILED" if sent else "NOT_SENT",
                    input_tokens=0,
                    output_tokens=0,
                    model_alias="embedding-main",
                    job_id=job_id,
                )
                recorded = True
                raise
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
            from app.qa.model_gateway import ModelGateway
            ModelGateway._record_call(
                tenant_id=tenant_id or os.getenv("RAG_DEFAULT_TENANT_ID", "tenant-demo-1"),
                call_id=call_id,
                run_id=run_id,
                purpose="EMBEDDING",
                status="SUCCESS",
                input_tokens=input_tokens,
                output_tokens=0,
                model_alias="embedding-main",
                job_id=job_id,
            )
            recorded = True

        return vectors
    except ApiException:
        if "call_id" in locals() and "recorded" in locals() and not recorded:
            try:
                from app.qa.model_gateway import ModelGateway
                ModelGateway._record_call(
                    tenant_id=tenant_id or os.getenv("RAG_DEFAULT_TENANT_ID", "tenant-demo-1"),
                    call_id=call_id,
                    run_id=run_id,
                    purpose="EMBEDDING",
                    status="FAILED" if ("sent" in locals() and sent) else "NOT_SENT",
                    input_tokens=0,
                    output_tokens=0,
                    model_alias="embedding-main",
                    job_id=job_id,
                )
            except Exception:
                logger.exception("Failed to reconcile embedding budget after validation error")
        raise
    except Exception as exc:
        logger.error("大模型嵌入网关连接失败: %s", exc)
        if not is_test_env():
            raise ApiException(503, "MODEL_GATEWAY_UNAVAILABLE", f"大模型嵌入网关连接失败: {exc}") from exc
        return [generate_deterministic_embedding(t, dim) for t in texts]
