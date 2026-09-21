from app.knowledge.chunker import chunk_units


def test_oversized_source_unit_is_hard_bounded():
    text = "第一句。" * 250
    units = [{
        "id": "u-long",
        "unit_key": "p1",
        "kind": "TEXT",
        "text_content": text,
        "location": {"page": 1},
    }]
    chunks, spans = chunk_units(units, max_parent_chars=2000, max_child_chars=400)
    children = [c for c in chunks if c["kind"] == "CHILD"]
    assert len(children) > 1
    assert all(len(c["text_content"]) <= 400 for c in children)
    assert spans
    assert min(s["start_cp"] for s in spans) == 0
    assert max(s["end_cp"] for s in spans) == len(text)
