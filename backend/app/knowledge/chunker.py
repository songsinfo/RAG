import hashlib
import uuid
from typing import Any, Dict, List, Tuple


def _heading_key(item: Dict[str, Any]) -> Tuple[str, ...]:
    headings = item.get("headings")
    if not isinstance(headings, list):
        return ()
    return tuple(str(value).strip() for value in headings if str(value).strip())


def _location(item: Dict[str, Any]) -> Dict[str, Any]:
    location: Dict[str, Any] = {
        "doclingChunkIndex": int(item.get("chunk_index") or 0),
        "headings": list(_heading_key(item)),
        "captions": list(item.get("captions") or []),
        "docItems": list(item.get("doc_items") or []),
        "pageNumbers": list(item.get("page_numbers") or []),
    }
    metadata = item.get("metadata")
    if isinstance(metadata, dict) and metadata:
        location["metadata"] = metadata
    return location


def adapt_docling_hybrid_chunks(
    items: List[Dict[str, Any]],
    *,
    parent_max_tokens: int = 1200,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Adapt Docling HybridChunker output to the enterprise provenance schema.

    HybridChunker owns all child splitting/token sizing. This adapter only groups
    adjacent child chunks into larger retrieval parents and maps Docling metadata
    into SourceUnit/ChunkSpan records.
    """
    if not items:
        return [], [], []

    units: List[Dict[str, Any]] = []
    chunks: List[Dict[str, Any]] = []
    spans: List[Dict[str, Any]] = []

    normalized: List[Dict[str, Any]] = []
    for position, item in enumerate(items):
        text = str(item.get("text") or "").strip()
        raw_text = str(item.get("raw_text") or text).strip()
        if not text:
            continue
        if not raw_text:
            raw_text = text
        unit_id = str(uuid.uuid4())
        location = _location(item)
        units.append(
            {
                "id": unit_id,
                "unit_key": f"docling_chunk_{int(item.get('chunk_index') if item.get('chunk_index') is not None else position)}",
                "kind": "TEXT",
                "text_content": raw_text,
                "location": location,
                "content_hash": hashlib.sha256(raw_text.encode("utf-8")).digest(),
            }
        )
        normalized.append(
            {
                "unit_id": unit_id,
                "text": text,
                "raw_text": raw_text,
                "num_tokens": max(1, int(item.get("num_tokens") or max(1, len(text) // 4))),
                "headings": _heading_key(item),
                "location": location,
            }
        )

    parent_ordinal = 0
    child_ordinal = 0
    group: List[Dict[str, Any]] = []
    group_tokens = 0
    group_heading: Tuple[str, ...] | None = None

    def flush_group() -> None:
        nonlocal parent_ordinal, child_ordinal, group, group_tokens, group_heading
        if not group:
            return
        parent_ordinal += 1
        parent_id = str(uuid.uuid4())
        parent_text = "\n\n".join(item["text"] for item in group)
        chunks.append(
            {
                "id": parent_id,
                "parent_id": None,
                "kind": "PARENT",
                "ordinal": parent_ordinal,
                "text_content": parent_text,
                "token_count": sum(item["num_tokens"] for item in group),
                "content_hash": hashlib.sha256(parent_text.encode("utf-8")).digest(),
            }
        )
        for item in group:
            child_ordinal += 1
            child_id = str(uuid.uuid4())
            chunks.append(
                {
                    "id": child_id,
                    "parent_id": parent_id,
                    "kind": "CHILD",
                    "ordinal": child_ordinal,
                    "text_content": item["text"],
                    "token_count": item["num_tokens"],
                    "content_hash": hashlib.sha256(item["text"].encode("utf-8")).digest(),
                }
            )
            spans.append(
                {
                    "id": str(uuid.uuid4()),
                    "chunk_id": child_id,
                    "unit_id": item["unit_id"],
                    "ordinal": 1,
                    "start_cp": 0,
                    "end_cp": len(item["raw_text"]),
                    "location": item["location"],
                }
            )
        group = []
        group_tokens = 0
        group_heading = None

    for item in normalized:
        heading = item["headings"]
        would_exceed = bool(group) and group_tokens + item["num_tokens"] > parent_max_tokens
        heading_changed = bool(group) and heading != group_heading
        if would_exceed or heading_changed:
            flush_group()
        if not group:
            group_heading = heading
        group.append(item)
        group_tokens += item["num_tokens"]
    flush_group()

    return units, chunks, spans
