import hashlib
import re
import uuid
from typing import Any, Dict, List, Tuple


def _split_text(text: str, max_chars: int) -> List[Tuple[int, int, str]]:
    """Split a single oversized source unit while preserving source offsets."""
    if len(text) <= max_chars:
        return [(0, len(text), text)]

    parts: List[Tuple[int, int, str]] = []
    start = 0
    while start < len(text):
        hard_end = min(len(text), start + max_chars)
        end = hard_end
        if hard_end < len(text):
            window = text[start:hard_end]
            # Prefer paragraph/sentence/whitespace boundaries in the back half.
            min_break = max(1, max_chars // 2)
            candidates = [
                window.rfind("\n\n", min_break),
                max(window.rfind("。", min_break), window.rfind("！", min_break), window.rfind("？", min_break)),
                max(window.rfind(". ", min_break), window.rfind("! ", min_break), window.rfind("? ", min_break)),
                window.rfind("\n", min_break),
                window.rfind(" ", min_break),
            ]
            best = max(candidates)
            if best > 0:
                token_len = 2 if window[best:best + 2] in {"\n\n", ". ", "! ", "? "} else 1
                end = start + best + token_len

        piece = text[start:end]
        if piece:
            parts.append((start, end, piece))
        start = end
    return parts


def chunk_units(
    units: List[Dict[str, Any]],
    max_parent_chars: int = 2000,
    max_child_chars: int = 400,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Structural parent-child chunking.

    Parent sections preserve document structure. Child chunks are hard-bounded by
    max_child_chars even when a single SourceUnit is larger than the target size.
    Chunk spans retain source-unit offsets so evidence provenance stays exact.
    """
    chunks: List[Dict[str, Any]] = []
    chunk_spans: List[Dict[str, Any]] = []
    current_parent_units: List[Dict[str, Any]] = []
    parent_ordinal = 0
    child_ordinal = 0

    def append_child(parent_id: str, text: str, spans: List[Dict[str, Any]]):
        nonlocal child_ordinal
        if not text:
            return
        child_ordinal += 1
        child_id = str(uuid.uuid4())
        chunks.append({
            "id": child_id,
            "parent_id": parent_id,
            "kind": "CHILD",
            "ordinal": child_ordinal,
            "text_content": text,
            "token_count": max(1, len(text) // 4),
            "content_hash": hashlib.sha256(text.encode("utf-8")).digest(),
        })
        for ordinal, span in enumerate(spans, start=1):
            chunk_spans.append({
                **span,
                "chunk_id": child_id,
                "ordinal": ordinal,
            })

    def flush_parent_and_children(parent_units: List[Dict[str, Any]]):
        nonlocal parent_ordinal
        if not parent_units:
            return

        parent_ordinal += 1
        parent_id = str(uuid.uuid4())
        combined_text = "\n\n".join(u["text_content"] for u in parent_units)
        chunks.append({
            "id": parent_id,
            "parent_id": None,
            "kind": "PARENT",
            "ordinal": parent_ordinal,
            "text_content": combined_text,
            "token_count": max(1, len(combined_text) // 4),
            "content_hash": hashlib.sha256(combined_text.encode("utf-8")).digest(),
        })

        child_text = ""
        child_spans: List[Dict[str, Any]] = []

        for unit in parent_units:
            unit_text = str(unit["text_content"])
            unit_id = unit["id"]

            for source_start, source_end, fragment in _split_text(unit_text, max_child_chars):
                separator = "\n" if child_text else ""
                proposed_len = len(child_text) + len(separator) + len(fragment)
                if child_text and proposed_len > max_child_chars:
                    append_child(parent_id, child_text, child_spans)
                    child_text = ""
                    child_spans = []
                    separator = ""

                # Oversized fragments are already hard-bounded by _split_text.
                if separator:
                    child_text += separator
                child_text += fragment
                child_spans.append({
                    "id": str(uuid.uuid4()),
                    "unit_id": unit_id,
                    "start_cp": source_start,
                    "end_cp": source_end,
                    "location": unit.get("location"),
                })

                if len(child_text) >= max_child_chars:
                    append_child(parent_id, child_text, child_spans)
                    child_text = ""
                    child_spans = []

        append_child(parent_id, child_text, child_spans)

    current_parent_len = 0
    for unit in units:
        is_major_heading = (
            unit["kind"] == "HEADING"
            and unit.get("location", {}).get("level", 1) <= 2
        )
        unit_len = len(unit["text_content"])

        if is_major_heading and current_parent_units:
            flush_parent_and_children(current_parent_units)
            current_parent_units = [unit]
            current_parent_len = unit_len
        elif current_parent_len + unit_len > max_parent_chars and current_parent_units:
            flush_parent_and_children(current_parent_units)
            current_parent_units = [unit]
            current_parent_len = unit_len
        else:
            current_parent_units.append(unit)
            current_parent_len += unit_len

    flush_parent_and_children(current_parent_units)
    return chunks, chunk_spans
