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


def save_artifact(
    tenant_id: str,
    filename: str,
    data: bytes,
    document_id: str | None = None,
    artifact_id: str | None = None,
) -> Tuple[str, bytes, int]:
    """Saves file to private directory and returns (storage_key, sha256_binary, size_bytes)."""
    sha = hashlib.sha256(data)
    sha_digest = sha.digest()
    sha_hex = sha.hexdigest()

    safe_name = Path(filename).name.replace(" ", "_")
    root = get_artifact_root()

    if document_id and artifact_id:
        storage_key = f"{tenant_id}/{document_id}/{artifact_id}/{safe_name}"
        dest = root / tenant_id / document_id / artifact_id / safe_name
    else:
        storage_key = f"{tenant_id}/{sha_hex}/{safe_name}"
        dest = root / tenant_id / sha_hex[:2] / sha_hex / safe_name

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)

    return storage_key, sha_digest, len(data)


def read_artifact(storage_key: str) -> bytes:
    root = get_artifact_root()
    target = (root / storage_key).resolve()
    if target.is_file() and target.is_relative_to(root.resolve()):
        return target.read_bytes()

    parts = storage_key.split("/")
    if len(parts) == 3:
        tenant_id, sha_hex, filename = parts
        legacy_target = (root / tenant_id / sha_hex[:2] / sha_hex / filename).resolve()
        if legacy_target.is_file() and legacy_target.is_relative_to(root.resolve()):
            return legacy_target.read_bytes()

    raise ApiException(404, "ARTIFACT_NOT_FOUND", "构件不存在")


def delete_artifact(storage_key: str):
    root = get_artifact_root()
    target = (root / storage_key).resolve()
    if target.is_file() and target.is_relative_to(root.resolve()):
        try:
            target.unlink()
        except OSError:
            pass
        return

    parts = storage_key.split("/")
    if len(parts) == 3:
        tenant_id, sha_hex, filename = parts
        legacy_target = (root / tenant_id / sha_hex[:2] / sha_hex / filename).resolve()
        if legacy_target.is_file() and legacy_target.is_relative_to(root.resolve()):
            try:
                legacy_target.unlink()
            except OSError:
                pass

