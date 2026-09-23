from typing import Any, Dict, List, Optional, Tuple

from app.errors import ApiException
from app.qa.model_gateway import ModelGateway


def rerank_hits(
    tenant_id: str,
    run_id: Optional[str],
    query: str,
    job_id: Optional[str] = None,
    hits: List[Dict[str, Any]],
    model_alias: str,
    top_k: int,
    fail_open: bool,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    if not hits:
        return [], []

    try:
        ranked = ModelGateway.rerank(
            tenant_id=tenant_id,
            run_id=run_id,
            query=query,
            documents=[str(h.get("text") or "") for h in hits],
            top_n=min(top_k, len(hits)),
            model_alias=model_alias,
            job_id=job_id,
        )
    except ApiException as exc:
        if not fail_open:
            raise
        return hits[:top_k], [f"RERANK_FALLBACK:{exc.code}"]

    output: List[Dict[str, Any]] = []
    seen = set()
    for item in ranked:
        idx = item.get("index")
        if not isinstance(idx, int) or idx < 0 or idx >= len(hits) or idx in seen:
            continue
        seen.add(idx)
        hit = dict(hits[idx])
        hit["rerankScore"] = float(item.get("relevance_score", item.get("score", 0.0)) or 0.0)
        output.append(hit)

    if not output:
        if fail_open:
            return hits[:top_k], ["RERANK_FALLBACK:INVALID_MODEL_OUTPUT"]
        raise ApiException(502, "INVALID_MODEL_OUTPUT", "Reranker 未返回有效排序结果")

    return output[:top_k], []
