import hashlib
import uuid
from typing import List, Dict, Any, Tuple


def chunk_units(
    units: List[Dict[str, Any]],
    max_parent_chars: int = 2000,
    max_child_chars: int = 400
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Performs structural parent-child chunking on parsed document units.
    Returns (chunks, chunk_spans).
    """
    chunks: List[Dict[str, Any]] = []
    chunk_spans: List[Dict[str, Any]] = []

    current_parent_units: List[Dict[str, Any]] = []
    parent_ordinal = 0
    child_ordinal = 0

    def flush_parent_and_children(p_units: List[Dict[str, Any]]):
        nonlocal parent_ordinal, child_ordinal
        if not p_units:
            return

        parent_ordinal += 1
        parent_id = str(uuid.uuid4())
        combined_text = "\n\n".join(u["text_content"] for u in p_units)
        parent_hash = hashlib.sha256(combined_text.encode("utf-8")).digest()

        chunks.append({
            "id": parent_id,
            "parent_id": None,
            "kind": "PARENT",
            "ordinal": parent_ordinal,
            "text_content": combined_text,
            "token_count": len(combined_text) // 4,
            "content_hash": parent_hash,
        })

        # Generate child chunks for the units in this parent
        current_child_text = ""
        current_child_spans = []

        for u in p_units:
            u_text = u["text_content"]
            u_id = u["id"]
            u_len = len(u_text)

            # If adding unit exceeds max_child_chars, flush child first
            if current_child_text and (len(current_child_text) + len(u_text) > max_child_chars):
                child_ordinal += 1
                child_id = str(uuid.uuid4())
                c_hash = hashlib.sha256(current_child_text.encode("utf-8")).digest()
                chunks.append({
                    "id": child_id,
                    "parent_id": parent_id,
                    "kind": "CHILD",
                    "ordinal": child_ordinal,
                    "text_content": current_child_text,
                    "token_count": len(current_child_text) // 4,
                    "content_hash": c_hash,
                })
                for sp_ord, sp in enumerate(current_child_spans, start=1):
                    sp["chunk_id"] = child_id
                    sp["ordinal"] = sp_ord
                    chunk_spans.append(sp)

                current_child_text = ""
                current_child_spans = []

            # Add unit to child
            start_in_child = len(current_child_text)
            if current_child_text:
                current_child_text += "\n"
                start_in_child += 1
            current_child_text += u_text

            current_child_spans.append({
                "id": str(uuid.uuid4()),
                "unit_id": u_id,
                "start_cp": 0,
                "end_cp": u_len,
                "location": u.get("location"),
            })

        if current_child_text:
            child_ordinal += 1
            child_id = str(uuid.uuid4())
            c_hash = hashlib.sha256(current_child_text.encode("utf-8")).digest()
            chunks.append({
                "id": child_id,
                "parent_id": parent_id,
                "kind": "CHILD",
                "ordinal": child_ordinal,
                "text_content": current_child_text,
                "token_count": len(current_child_text) // 4,
                "content_hash": c_hash,
            })
            for sp_ord, sp in enumerate(current_child_spans, start=1):
                sp["chunk_id"] = child_id
                sp["ordinal"] = sp_ord
                chunk_spans.append(sp)

    current_parent_len = 0
    for unit in units:
        # If unit is high-level heading (H1, H2), it starts a new parent section
        is_major_heading = unit["kind"] == "HEADING" and unit.get("location", {}).get("level", 1) <= 2
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
