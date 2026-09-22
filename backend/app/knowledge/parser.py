import hashlib
import mimetypes
import os
from pathlib import Path
from typing import Any, Dict, List, Tuple

import httpx

from app.config import settings
from app.errors import ApiException
from app.knowledge.chunker import adapt_docling_hybrid_chunks


def _bool_form(value: bool) -> str:
    return "true" if value else "false"


def _docling_hybrid_chunks(
    file_bytes: bytes,
    filename: str,
    *,
    max_tokens: int,
    tokenizer: str,
    merge_peers: bool,
    vision: bool,
) -> List[Dict[str, Any]]:
    """Parse and chunk every supported input through Docling HybridChunker."""
    base_url = (os.getenv("DOCLING_URL") or settings.DOCLING_URL).rstrip("/")
    api_key = os.getenv("DOCLING_API_KEY") or settings.DOCLING_API_KEY
    if not base_url:
        raise ApiException(503, "DOCLING_UNAVAILABLE", "未配置 Docling 服务地址")

    headers = {"X-Api-Key": api_key} if api_key else {}
    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    data = {
        # Conversion options. Docling applies the relevant options per input format.
        "convert_do_ocr": "true",
        "convert_do_table_structure": "true",
        "convert_table_mode": "accurate",
        "convert_abort_on_error": "true",
        "convert_do_pdf_heading_hierarchy": "true",
        "convert_do_picture_classification": _bool_form(vision),
        "convert_do_picture_description": _bool_form(vision),
        # HybridChunker options.
        "chunking_use_markdown_tables": "true",
        "chunking_include_raw_text": "true",
        "chunking_max_tokens": str(max_tokens),
        "chunking_merge_peers": _bool_form(merge_peers),
        "include_converted_doc": "false",
        "target_type": "inbody",
    }
    if tokenizer:
        data["chunking_tokenizer"] = tokenizer

    try:
        with httpx.Client(timeout=httpx.Timeout(180.0, connect=5.0)) as client:
            response = client.post(
                f"{base_url}/v1/chunk/hybrid/file",
                headers=headers,
                files={"files": (Path(filename).name, file_bytes, media_type)},
                data=data,
            )
    except Exception as exc:
        raise ApiException(503, "DOCLING_UNAVAILABLE", f"Docling 连接失败: {exc}") from exc

    if response.status_code != 200:
        detail = ""
        try:
            payload = response.json()
            detail = str(payload.get("detail") or payload.get("error") or "")
        except Exception:
            detail = str(getattr(response, "text", "") or "")
        suffix = f": {detail[:300]}" if detail else ""
        raise ApiException(
            502,
            "DOCLING_PARSE_FAILED",
            f"Docling HybridChunker 处理失败，状态码 {response.status_code}{suffix}",
        )

    try:
        payload = response.json()
    except Exception as exc:
        raise ApiException(502, "DOCLING_INVALID_RESPONSE", "Docling 未返回合法 JSON") from exc

    chunks = payload.get("chunks") if isinstance(payload, dict) else None
    if not isinstance(chunks, list) or not chunks:
        raise ApiException(
            502,
            "DOCLING_INVALID_RESPONSE",
            "Docling HybridChunker 未返回有效 chunks",
        )

    warnings = payload.get("warnings") if isinstance(payload, dict) else None
    if isinstance(warnings, list) and warnings:
        first = dict(chunks[0])
        metadata = dict(first.get("metadata") or {})
        metadata["doclingWarnings"] = warnings[:100]
        first["metadata"] = metadata
        chunks = [first, *chunks[1:]]
    return chunks


def parse_and_chunk_document(
    file_bytes: bytes,
    filename: str,
    build_config: Dict[str, Any] | None = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Unified ingestion boundary: Docling parses and HybridChunker chunks all files."""
    cfg = build_config or {}
    max_tokens = int(
        cfg.get("chunkMaxTokens")
        or os.getenv("DOCLING_CHUNK_MAX_TOKENS")
        or settings.DOCLING_CHUNK_MAX_TOKENS
    )
    max_tokens = max(64, min(4096, max_tokens))
    parent_max_tokens = int(
        cfg.get("parentMaxTokens")
        or os.getenv("DOCLING_PARENT_MAX_TOKENS")
        or settings.DOCLING_PARENT_MAX_TOKENS
    )
    parent_max_tokens = max(max_tokens, min(8192, parent_max_tokens))
    tokenizer = str(
        cfg.get("chunkTokenizer")
        or os.getenv("DOCLING_CHUNK_TOKENIZER")
        or settings.DOCLING_CHUNK_TOKENIZER
        or ""
    ).strip()
    merge_peers = str(
        cfg.get("mergePeers")
        if "mergePeers" in cfg
        else os.getenv("DOCLING_CHUNK_MERGE_PEERS", str(settings.DOCLING_CHUNK_MERGE_PEERS))
    ).lower() in {"1", "true", "yes", "on"}

    docling_chunks = _docling_hybrid_chunks(
        file_bytes,
        filename,
        max_tokens=max_tokens,
        tokenizer=tokenizer,
        merge_peers=merge_peers,
        vision=bool(cfg.get("vision", False)),
    )
    return adapt_docling_hybrid_chunks(
        docling_chunks,
        parent_max_tokens=parent_max_tokens,
    )


def parse_document(
    file_bytes: bytes,
    filename: str,
    build_config: Dict[str, Any] | None = None,
) -> List[Dict[str, Any]]:
    """Compatibility wrapper. New ingestion should use parse_and_chunk_document."""
    units, _, _ = parse_and_chunk_document(file_bytes, filename, build_config)
    return units
