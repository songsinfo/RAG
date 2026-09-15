import hashlib
import re
from typing import List, Dict, Any
from app.errors import ApiException


def decode_utf8(data: bytes) -> str:
    try:
        text = data.decode("utf-8")
        if text.startswith("\ufeff"):
            text = text[1:]
        return text
    except UnicodeDecodeError:
        raise ApiException(422, "ENCODING_INVALID", "文档必须为 UTF-8")


def parse_markdown(text: str) -> List[Dict[str, Any]]:
    lines = text.splitlines()
    units: List[Dict[str, Any]] = []
    unit_counter = 0

    in_code_block = False
    code_lines: List[str] = []
    code_start_line = 0

    table_lines: List[str] = []
    table_start_line = 0

    current_para: List[str] = []
    para_start_line = 0

    def flush_para():
        nonlocal current_para, unit_counter
        if current_para:
            unit_counter += 1
            content = "\n".join(current_para).strip()
            if content:
                units.append({
                    "unit_key": f"p_{unit_counter}",
                    "kind": "TEXT",
                    "text_content": content,
                    "location": {"start_line": para_start_line, "end_line": para_start_line + len(current_para) - 1},
                })
            current_para = []

    def flush_table():
        nonlocal table_lines, unit_counter
        if table_lines:
            unit_counter += 1
            content = "\n".join(table_lines).strip()
            units.append({
                "unit_key": f"tbl_{unit_counter}",
                "kind": "TABLE",
                "text_content": content,
                "location": {"start_line": table_start_line, "end_line": table_start_line + len(table_lines) - 1},
            })
            table_lines = []

    for idx, line in enumerate(lines, start=1):
        stripped = line.strip()

        # Check code block fences
        if stripped.startswith("```"):
            if in_code_block:
                code_lines.append(line)
                unit_counter += 1
                units.append({
                    "unit_key": f"code_{unit_counter}",
                    "kind": "CODE",
                    "text_content": "\n".join(code_lines),
                    "location": {"start_line": code_start_line, "end_line": idx},
                })
                in_code_block = False
                code_lines = []
            else:
                flush_para()
                flush_table()
                in_code_block = True
                code_lines = [line]
                code_start_line = idx
            continue

        if in_code_block:
            code_lines.append(line)
            continue

        # Check table line
        if stripped.startswith("|") and stripped.endswith("|"):
            flush_para()
            if not table_lines:
                table_start_line = idx
            table_lines.append(line)
            continue
        elif table_lines:
            flush_table()

        # Check heading
        heading_match = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading_match:
            flush_para()
            unit_counter += 1
            level = len(heading_match.group(1))
            title = heading_match.group(2).strip()
            units.append({
                "unit_key": f"h_{unit_counter}",
                "kind": "HEADING",
                "text_content": title,
                "location": {"line": idx, "level": level},
            })
            continue

        # Blank lines separate paragraphs
        if not stripped:
            flush_para()
            continue

        if not current_para:
            para_start_line = idx
        current_para.append(line)

    flush_para()
    flush_table()

    # Add content_hash to each unit
    for u in units:
        u["content_hash"] = hashlib.sha256(u["text_content"].encode("utf-8")).digest()

    return units


def parse_document(file_bytes: bytes, filename: str) -> List[Dict[str, Any]]:
    text = decode_utf8(file_bytes)
    # Default to markdown parser for .md, .txt, etc.
    return parse_markdown(text)
