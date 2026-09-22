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


def _group_location(group: List[Dict[str, Any]]) -> Dict[str, Any]:
    headings = list(group[0]["headings"]) if group else []
    pages = sorted({
        int(page)
        for item in group
        for page in item["location"].get("pageNumbers", [])
        if isinstance(page, int)
    })
    doc_items = [
        str(value)
        for item in group
        for value in item["location"].get("docItems", [])
    ]
    captions = [
        str(value)
        for item in group
        for value in item["location"].get("captions", [])
    ]
    return {
        "headings": headings,
        "pageNumbers": pages,
        "docItems": doc_items,
        "captions": captions,
        "doclingChunkStart": int(group[0]["chunk_index"]) if group else 0,
        "doclingChunkEnd": int(group[-1]["chunk_index"]) if group else 0,
    }


def adapt_docling_hybrid_chunks(
    items: List[Dict[str, Any]],
    *,
    parent_max_tokens: int = 1200,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Adapt Docling HybridChunker output to the enterprise provenance schema.

    Docling HybridChunker owns all child splitting and token sizing. This adapter
    only groups adjacent children with the same heading path into bounded parent
    contexts and SourceUnits. That preserves read_section/provenance without
    re-splitting HybridChunker output.
    """
    if not items:
        return [], [], []

    normalized: List[Dict[str, Any]] = []
    for position, item in enumerate(items):
        text = str(item.get("text") or "").strip()
        raw_text = str(item.get("raw_text") or text).strip()
        if not text:
            continue
        if not raw_text:
            raw_text = text
        normalized.append(
            {
                "chunk_index": int(
                    item.get("chunk_index")
                    if item.get("chunk_index") is not None
                    else position
                ),
                "text": text,
                "raw_text": raw_text,
                "num_tokens": max(
                    1,
                    int(item.get("num_tokens") or max(1, len(text) // 4)),
                ),
                "headings": _heading_key(item),
                "location": _location(item),
            }
        )

    groups: List[List[Dict[str, Any]]] = []
    group: List[Dict[str, Any]] = []
    group_tokens = 0
    group_heading: Tuple[str, ...] | None = None

    for item in normalized:
        heading = item["headings"]
        would_exceed = bool(group) and group_tokens + item["num_tokens"] > parent_max_tokens
        heading_changed = bool(group) and heading != group_heading
        if would_exceed or heading_changed:
            groups.append(group)
            group = []
            group_tokens = 0
            group_heading = None
        if not group:
            group_heading = heading
        group.append(item)
        group_tokens += item["num_tokens"]
    if group:
        groups.append(group)

    units: List[Dict[str, Any]] = []
    chunks: List[Dict[str, Any]] = []
    spans: List[Dict[str, Any]] = []
    child_ordinal = 0

    for parent_ordinal, group in enumerate(groups, start=1):
        source_unit_id = str(uuid.uuid4())
        raw_parts = [item["raw_text"] for item in group]
        source_text = "\n\n".join(raw_parts)
        location = _group_location(group)
        units.append(
            {
                "id": source_unit_id,
                "unit_key": f"docling_section_{parent_ordinal}",
                "kind": "TEXT",
                "text_content": source_text,
                "location": location,
                "content_hash": hashlib.sha256(source_text.encode("utf-8")).digest(),
            }
        )

        headings = list(group[0]["headings"])
        heading_prefix = "\n".join(headings).strip()
        parent_body = source_text
        parent_text = (
            f"{heading_prefix}\n{parent_body}".strip()
            if heading_prefix
            else parent_body
        )
        parent_id = str(uuid.uuid4())
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

        cursor = 0
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
            start = cursor
            end = start + len(item["raw_text"])
            spans.append(
                {
                    "id": str(uuid.uuid4()),
                    "chunk_id": child_id,
                    "unit_id": source_unit_id,
                    "ordinal": 1,
                    "start_cp": start,
                    "end_cp": end,
                    "location": item["location"],
                }
            )
            cursor = end + 2  # "\n\n" separator in source_text

    return units, chunks, spans
