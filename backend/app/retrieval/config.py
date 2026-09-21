from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional

from sqlalchemy import and_, select

from app.db import get_db_connection
from app.errors import ApiException
from app.models import config_revision_table


DEFAULT_RETRIEVAL_CONFIG_ID = "cfg-enterprise-retrieval-v2"


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


@dataclass(frozen=True)
class RetrievalConfig:
    config_id: str
    dense_top_k: int = 30
    sparse_top_k: int = 30
    fusion_top_k: int = 20
    rrf_k: int = 60
    final_top_k: int = 8
    rerank_enabled: bool = False
    rerank_top_k: int = 10
    rerank_model: str = "rerank-main"
    rerank_fail_open: bool = True
    parent_expansion_enabled: bool = True
    parent_max_chars: int = 6000

    @classmethod
    def from_payload(cls, config_id: str, payload: Optional[Dict[str, Any]]) -> "RetrievalConfig":
        root = payload if isinstance(payload, dict) else {}
        retrieval = root.get("retrieval") if isinstance(root.get("retrieval"), dict) else root
        rerank = root.get("rerank") if isinstance(root.get("rerank"), dict) else {}
        parent = root.get("parentExpansion") if isinstance(root.get("parentExpansion"), dict) else {}

        # Backward compatibility with the original {retrieval:{topK,rrfK}} payload.
        legacy_top_k = _bounded_int(retrieval.get("topK"), 10, 1, 100)
        dense_top_k = _bounded_int(retrieval.get("denseTopK"), max(legacy_top_k * 2, 10), 1, 200)
        sparse_top_k = _bounded_int(retrieval.get("sparseTopK"), max(legacy_top_k * 2, 10), 1, 200)
        fusion_top_k = _bounded_int(retrieval.get("fusionTopK"), legacy_top_k, 1, 100)
        final_top_k = _bounded_int(retrieval.get("finalTopK"), min(legacy_top_k, 8), 1, 50)

        return cls(
            config_id=config_id,
            dense_top_k=dense_top_k,
            sparse_top_k=sparse_top_k,
            fusion_top_k=fusion_top_k,
            rrf_k=_bounded_int(retrieval.get("rrfK"), 60, 1, 500),
            final_top_k=final_top_k,
            rerank_enabled=_as_bool(rerank.get("enabled"), False),
            rerank_top_k=_bounded_int(rerank.get("topK"), min(fusion_top_k, 10), 1, 50),
            rerank_model=str(rerank.get("model") or "rerank-main"),
            rerank_fail_open=_as_bool(rerank.get("failOpen"), True),
            parent_expansion_enabled=_as_bool(parent.get("enabled"), True),
            parent_max_chars=_bounded_int(parent.get("maxChars"), 6000, 500, 20000),
        )

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def load_retrieval_config(tenant_id: str, config_id: Optional[str]) -> RetrievalConfig:
    effective_id = config_id or DEFAULT_RETRIEVAL_CONFIG_ID
    with get_db_connection() as conn:
        row = conn.execute(
            select(
                config_revision_table.c.kind,
                config_revision_table.c.status,
                config_revision_table.c.payload,
            ).where(
                and_(
                    config_revision_table.c.tenant_id == tenant_id,
                    config_revision_table.c.id == effective_id,
                )
            )
        ).first()

    if not row:
        # Keep local/test bootstrap usable while still making the effective config explicit.
        if effective_id in {DEFAULT_RETRIEVAL_CONFIG_ID, "cfg-default-retrieval"}:
            return RetrievalConfig(config_id=effective_id)
        raise ApiException(409, "RETRIEVAL_CONFIG_NOT_FOUND", "检索配置不存在或已失效")

    if str(row.kind).upper() != "RETRIEVAL":
        raise ApiException(409, "INVALID_RETRIEVAL_CONFIG", "配置类型不是 RETRIEVAL")
    if str(row.status).upper() not in {"ACTIVE", "PUBLISHED"}:
        raise ApiException(409, "RETRIEVAL_CONFIG_NOT_PUBLISHED", "检索配置尚未发布")

    return RetrievalConfig.from_payload(effective_id, row.payload)


def validate_retrieval_payload(payload: Dict[str, Any]) -> RetrievalConfig:
    if not isinstance(payload, dict):
        raise ApiException(400, "INVALID_CONFIG", "检索配置必须是 JSON 对象")
    config = RetrievalConfig.from_payload("validation", payload)
    if config.final_top_k > config.fusion_top_k:
        raise ApiException(400, "INVALID_CONFIG", "finalTopK 不能大于 fusionTopK")
    if config.rerank_enabled and config.rerank_top_k > config.fusion_top_k:
        raise ApiException(400, "INVALID_CONFIG", "rerank.topK 不能大于 fusionTopK")
    return config
