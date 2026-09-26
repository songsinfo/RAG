from collections import defaultdict
from typing import List, Dict, Any


def rrf_fuse(
    ranked_lists: List[List[Dict[str, Any]]],
    weights: List[float] = None,
    rrf_k: int = 60,
) -> List[Dict[str, Any]]:
    """
    Reciprocal Rank Fusion (RRF) merges multiple ranked candidate hit lists.
    Each item in ranked_lists must contain at least 'id'.
    """
    if not ranked_lists:
        return []

    if weights is None:
        weights = [1.0] * len(ranked_lists)

    scores: Dict[str, float] = defaultdict(float)
    items_by_id: Dict[str, Dict[str, Any]] = {}

    for route_idx, route_hits in enumerate(ranked_lists):
        w = weights[route_idx]
        seen = set()
        unique_rank = 0
        for hit in route_hits:
            hit_id = hit["id"]
            if hit_id in seen:
                continue
            seen.add(hit_id)
            unique_rank += 1
            if hit_id not in items_by_id:
                items_by_id[hit_id] = dict(hit)
            scores[hit_id] += w / (rrf_k + unique_rank)

    sorted_ids = sorted(scores.keys(), key=lambda x: (-scores[x], str(x)))
    fused: List[Dict[str, Any]] = []
    for sid in sorted_ids:
        item = items_by_id[sid]
        item["score"] = scores[sid]
        fused.append(item)

    return fused
