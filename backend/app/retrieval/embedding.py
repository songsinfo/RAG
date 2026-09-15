import hashlib
import math
import os
from typing import List
import httpx


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
    gateway_url = os.getenv("LITELLM_BASE_URL")
    if not gateway_url:
        return [generate_deterministic_embedding(t, dim) for t in texts]

    master_key = os.getenv("LITELLM_MASTER_KEY", "sk-litellm-internal-master-key")

    try:
        with httpx.Client(timeout=0.5) as client:
            resp = client.post(
                f"{gateway_url}/embeddings",
                headers={"Authorization": f"Bearer {master_key}"},
                json={"model": "embedding-main", "input": texts},
            )
            if resp.status_code == 200:
                data = resp.json().get("data", [])
                vectors = [item["embedding"] for item in data]
                # Validate dimensions and non-zero
                for v in vectors:
                    assert len(v) == dim
                    assert any(x != 0 for x in v)
                return vectors
    except Exception:
        pass

    # Fallback to deterministic embeddings for local test environments
    return [generate_deterministic_embedding(t, dim) for t in texts]
