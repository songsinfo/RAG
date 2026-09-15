import hashlib
import os
from pathlib import Path
from typing import Tuple
from app.errors import ApiException


def get_artifact_root() -> Path:
    root_str = os.getenv("RAG_ARTIFACT_ROOT", "")
    if root_str:
        p = Path(root_str).resolve()
    else:
        rag_root = Path(__file__).resolve().parent.parent.parent.parent
        p = rag_root / ".runtime" / os.getenv("RAG_ENVIRONMENT", "demo") / "artifacts"
    p.mkdir(parents=True, exist_ok=True)
    return p


def save_artifact(tenant_id: str, filename: str, data: bytes) -> Tuple[str, bytes, int]:
    """Saves file to private directory and returns (storage_key, sha256_binary, size_bytes)."""
    sha = hashlib.sha256(data)
    sha_digest = sha.digest()
    sha_hex = sha.hexdigest()

    safe_name = Path(filename).name.replace(" ", "_")
    root = get_artifact_root()
    tenant_dir = root / tenant_id / sha_hex[:2] / sha_hex
    tenant_dir.mkdir(parents=True, exist_ok=True)

    dest = tenant_dir / safe_name
    dest.write_bytes(data)

    # storage_key is relative or URI-like
    storage_key = f"{tenant_id}/{sha_hex}/{safe_name}"
    return storage_key, sha_digest, len(data)


def read_artifact(storage_key: str) -> bytes:
    root = get_artifact_root()
    parts = storage_key.split("/")
    if len(parts) != 3:
        raise ApiException(404, "ARTIFACT_NOT_FOUND", "构件不存在")
    tenant_id, sha_hex, filename = parts
    target = root / tenant_id / sha_hex[:2] / sha_hex / filename
    if not target.exists():
        raise ApiException(404, "ARTIFACT_NOT_FOUND", "构件不存在")
    return target.read_bytes()
