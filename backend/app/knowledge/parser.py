import hashlib
import mimetypes
import os
import re
from pathlib import Path
from typing import Any, Dict, List

import httpx

from app.config import settings, is_test_env
from app.errors import ApiException

_TEXT_EXTENSIONS = {".md", ".markdown", ".txt"}


def decode_utf8(data: bytes) -> str:
    try:
        text = data.decode("utf-8")
        return text[1:] if text.startswith("\ufeff") else text
    except UnicodeDecodeError:
        raise ApiException(422, "ENCODING_INVALID", "文本类文档必须为 UTF-8")


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
                units.append({"unit_key": f"p_{unit_counter}", "kind": "TEXT", "text_content": content,
                              "location": {"start_line": para_start_line, "end_line": para_start_line + len(current_para) - 1}})
            current_para = []

    def flush_table():
        nonlocal table_lines, unit_counter
        if table_lines:
            unit_counter += 1
            units.append({"unit_key": f"tbl_{unit_counter}", "kind": "TABLE",
                          "text_content": "\n".join(table_lines).strip(),
                          "location": {"start_line": table_start_line, "end_line": table_start_line + len(table_lines) - 1}})
            table_lines = []

    for idx, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_code_block:
                code_lines.append(line)
                unit_counter += 1
                units.append({"unit_key": f"code_{unit_counter}", "kind": "CODE",
                              "text_content": "\n".join(code_lines),
                              "location": {"start_line": code_start_line, "end_line": idx}})
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
        if stripped.startswith("|") and stripped.endswith("|"):
            flush_para()
            if not table_lines:
                table_start_line = idx
            table_lines.append(line)
            continue
        elif table_lines:
            flush_table()
        heading_match = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading_match:
            flush_para()
            unit_counter += 1
            units.append({"unit_key": f"h_{unit_counter}", "kind": "HEADING",
                          "text_content": heading_match.group(2).strip(),
                          "location": {"line": idx, "level": len(heading_match.group(1))}})
            continue
        if not stripped:
            flush_para()
            continue
        if not current_para:
            para_start_line = idx
        current_para.append(line)

    flush_para()
    flush_table()
    for unit in units:
        unit["content_hash"] = hashlib.sha256(unit["text_content"].encode("utf-8")).digest()
    return units


def _docling_markdown(file_bytes: bytes, filename: str) -> str:
    base_url = (os.getenv("DOCLING_URL") or settings.DOCLING_URL).rstrip("/")
    api_key = os.getenv("DOCLING_API_KEY") or settings.DOCLING_API_KEY
    if not base_url:
        raise ApiException(503, "DOCLING_UNAVAILABLE", "未配置 Docling 服务地址")
    headers = {"X-Api-Key": api_key} if api_key else {}
    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    try:
        with httpx.Client(timeout=httpx.Timeout(120.0, connect=5.0)) as client:
            response = client.post(
                f"{base_url}/v1/convert/file",
                headers=headers,
                files={"files": (Path(filename).name, file_bytes, media_type)},
                data={"to_formats": "md", "do_ocr": "true", "do_table_structure": "true",
                      "table_mode": "accurate", "abort_on_error": "true"},
            )
    except Exception as exc:
        raise ApiException(503, "DOCLING_UNAVAILABLE", f"Docling 连接失败: {exc}")
    if response.status_code != 200:
        raise ApiException(502, "DOCLING_PARSE_FAILED", f"Docling 解析失败，状态码 {response.status_code}")
    payload = response.json()
    document = payload.get("document") if isinstance(payload, dict) else None
    markdown = document.get("md_content") if isinstance(document, dict) else None
    if not isinstance(markdown, str) or not markdown.strip():
        raise ApiException(502, "DOCLING_INVALID_RESPONSE", "Docling 未返回有效 Markdown 内容")
    return markdown


def parse_document(file_bytes: bytes, filename: str) -> List[Dict[str, Any]]:
    suffix = Path(filename).suffix.lower()
    if suffix in _TEXT_EXTENSIONS:
        return parse_markdown(decode_utf8(file_bytes))
    if is_test_env() and not (os.getenv("DOCLING_URL") or "").strip():
        raise ApiException(503, "DOCLING_UNAVAILABLE", "测试环境未配置 Docling 服务")
    return parse_markdown(_docling_markdown(file_bytes, filename))
