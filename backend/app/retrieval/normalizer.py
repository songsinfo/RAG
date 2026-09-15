import re
from typing import List


def normalize_terms(text: str) -> str:
    """
    Normalizes text to optimize BM25 keyword matching for:
    - snake_case (e.g. active_build_id -> active_build_id active build id)
    - camelCase (e.g. expectedVersion -> expectedVersion expected version)
    - paths (e.g. backend/src/main.py -> backend src main py)
    - error codes (e.g. INVALID_MODEL_RESPONSE -> INVALID_MODEL_RESPONSE invalid model response)
    """
    if not text:
        return ""

    tokens: List[str] = []

    # 1. Match code identifiers and paths
    # Match paths
    paths = re.findall(r"[\w.-]+(?:/[\w.-]+)+", text)
    for p in paths:
        tokens.append(p)
        tokens.extend(re.split(r"[/\\.-]", p))

    # Match snake_case and error codes
    snake_words = re.findall(r"[A-Za-z0-9]+(?:_[A-Za-z0-9]+)+", text)
    for sw in snake_words:
        tokens.append(sw)
        tokens.extend(sw.split("_"))

    # Match camelCase
    camel_words = re.findall(r"[a-z]+(?:[A-Z][a-z0-9]+)+", text)
    for cw in camel_words:
        tokens.append(cw)
        tokens.extend(re.findall(r"[a-z]+|[A-Z][a-z0-9]*", cw))

    # Clean empty and single-char non-ascii tokens
    cleaned = []
    seen = set()
    for t in tokens:
        t_clean = t.strip()
        if t_clean and t_clean.lower() not in seen:
            seen.add(t_clean.lower())
            cleaned.append(t_clean)

    return f"{text} {' '.join(cleaned)}".strip()
