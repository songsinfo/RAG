import math
from typing import List, Set, Dict, Any, Optional, Union


class EvaluationMetrics:
    """Core IR and QA evaluation metrics algorithms."""

    @staticmethod
    def ratio(numerator: int, denominator: int) -> Optional[float]:
        """Safely compute proportion. Returns None if denominator is 0."""
        return None if denominator == 0 else float(numerator) / denominator

    @staticmethod
    def retrieval(
        groups: List[Set[str]],
        ranked: List[Set[str]],
        k: int = 5,
        ranked_grades: Optional[List[int]] = None,
        ideal_grades: Optional[List[int]] = None,
    ) -> Dict[str, Optional[float]]:
        """
        Compute Hit@K, Recall@K, MRR@K, and optionally NDCG@K against gold fact groups.

        Each fact group is a set of equivalent alternative anchor identifiers.
        A rank position hits a fact group if their sets intersect.
        """
        if k < 1:
            raise ValueError("k must be positive")
        result: Dict[str, Optional[float]] = {}
        if not groups:
            result["hit"] = None
            result["recall"] = None
            result["mrr"] = None
            if ranked_grades is not None and ideal_grades is not None:
                result["ndcg"] = None
            return result

        if any(len(g) == 0 for g in groups):
            raise ValueError("Empty fact group")

        found: Set[int] = set()
        first_relevant_rank = 0

        for rank in range(min(k, len(ranked))):
            relevant = False
            for g_idx, g_set in enumerate(groups):
                if not g_set.isdisjoint(ranked[rank]):
                    found.add(g_idx)
                    relevant = True
            if relevant and first_relevant_rank == 0:
                first_relevant_rank = rank + 1

        result["hit"] = 1.0 if found else 0.0
        result["recall"] = float(len(found)) / len(groups)
        result["mrr"] = (1.0 / first_relevant_rank) if first_relevant_rank > 0 else 0.0

        if ranked_grades is not None and ideal_grades is not None:
            result["ndcg"] = EvaluationMetrics.ndcg(ranked_grades, ideal_grades, k)

        return result

    @staticmethod
    def ndcg(ranked: List[int], ideal: List[int], k: int = 5) -> Optional[float]:
        """
        Compute Normalized Discounted Cumulative Gain (NDCG@K) for graded relevance (0..3).
        Formula: DCG@K / IDCG@K, with exponential gain (2^r - 1) and log2 position discount.
        """
        if k < 1:
            raise ValueError("k must be positive")
        if any(g < 0 or g > 3 for g in ranked) or any(g < 0 or g > 3 for g in ideal):
            raise ValueError("Grade must be 0..3; unjudged is not zero")

        sorted_ideal = sorted(ideal, reverse=True)
        denominator = EvaluationMetrics.dcg(sorted_ideal, k)
        if denominator == 0.0:
            return None
        return EvaluationMetrics.dcg(ranked, k) / denominator

    @staticmethod
    def dcg(grades: List[int], k: int) -> float:
        """Compute Discounted Cumulative Gain (DCG@K)."""
        total = 0.0
        for i in range(min(k, len(grades))):
            total += (2.0 ** grades[i] - 1.0) / math.log2(i + 2)
        return total

    @staticmethod
    def percentile(values: List[Union[int, float]], quantile: float) -> Optional[Union[int, float]]:
        """Compute percentile with nearest-rank ceiling rule: index = ceil(N * quantile) - 1."""
        if quantile <= 0 or quantile > 1:
            raise ValueError("quantile must be (0,1]")
        if not values:
            return None
        sorted_vals = sorted(values)
        idx = math.ceil(len(sorted_vals) * quantile) - 1
        return sorted_vals[idx]
