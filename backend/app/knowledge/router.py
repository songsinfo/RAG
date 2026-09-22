import json
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, Depends, Header, UploadFile, File, Form, status, Response
from pydantic import BaseModel

from app.identity.deps import Actor, get_current_actor, require_admin
from app.errors import ApiException
from app.knowledge.service import KnowledgeService

router = APIRouter(tags=["knowledge"])


class CreateKbRequest(BaseModel):
    name: str
    readers: List[Dict[str, Any]] = []


class UpdateKbAclRequest(BaseModel):
    expectedVersion: int
    readers: List[Dict[str, Any]] = []


class UpdateDocAclRequest(BaseModel):
    expectedVersion: int
    mode: str = "RESTRICT"
    readers: List[Dict[str, Any]] = []


class LifecycleActionRequest(BaseModel):
    expectedVersion: int



# Knowledge Base endpoints
@router.get("/knowledge-bases")
def list_kbs(actor: Actor = Depends(get_current_actor)):
    return KnowledgeService.list_kbs(actor)


@router.post("/admin/knowledge-bases")
def create_kb(payload: CreateKbRequest, actor: Actor = Depends(require_admin)):
    return KnowledgeService.create_kb(actor, payload.name, payload.readers)


@router.get("/admin/knowledge-bases/{kb_id}/acl")
def get_kb_acl(kb_id: str, actor: Actor = Depends(require_admin)):
    return KnowledgeService.get_kb_acl(actor, kb_id)


@router.put("/admin/knowledge-bases/{kb_id}/acl")
def update_kb_acl(kb_id: str, payload: UpdateKbAclRequest, actor: Actor = Depends(require_admin)):
    return KnowledgeService.update_kb_acl(actor, kb_id, payload.expectedVersion, payload.readers)


# Document endpoints
@router.get("/documents")
def list_documents(kbId: Optional[str] = None, actor: Actor = Depends(get_current_actor)):
    return KnowledgeService.list_documents(actor, kb_id=kbId, admin_mode=False)


@router.get("/admin/documents")
def list_admin_documents(
    kbId: Optional[str] = None,
    status: str = "LIVE",
    actor: Actor = Depends(require_admin),
):
    return KnowledgeService.list_documents(actor, kb_id=kbId, admin_mode=True, status=status)


@router.get("/documents/{doc_id}")
def get_document(doc_id: str, actor: Actor = Depends(get_current_actor)):
    return KnowledgeService.get_document(actor, doc_id)


@router.post("/admin/documents", status_code=status.HTTP_202_ACCEPTED)
async def upload_document(
    file: UploadFile = File(...),
    kbId: str = Form(...),
    buildConfig: str = Form(default="{}"),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    actor: Actor = Depends(require_admin),
):
    content = await file.read()
    try:
        cfg = json.loads(buildConfig) if buildConfig else {}
    except Exception as exc:
        raise ApiException(400, "INVALID_CONFIG", "buildConfig 必须是合法 JSON") from exc
    if not isinstance(cfg, dict):
        raise ApiException(400, "INVALID_CONFIG", "buildConfig 必须是 JSON 对象")
    return KnowledgeService.upload_document(
        actor=actor,
        kb_id=kbId,
        document_id=None,
        filename=file.filename or "uploaded.txt",
        file_bytes=content,
        idempotency_key=idempotency_key,
        build_config=cfg,
    )


@router.post("/admin/documents/batch")
async def upload_documents_batch(
    files: List[UploadFile] = File(...),
    kbId: str = Form(...),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    actor: Actor = Depends(require_admin),
):
    if not files or len(files) > 100:
        raise ApiException(413, "INPUT_TOO_LARGE", "批量上传最多100个文件")
    results = []
    for index, file in enumerate(files):
        try:
            results.append(
                KnowledgeService.upload_document(
                    actor=actor,
                    kb_id=kbId,
                    document_id=None,
                    filename=file.filename or f"uploaded-{index}.txt",
                    file_bytes=await file.read(),
                    idempotency_key=f"{idempotency_key}-{index}",
                    build_config={},
                )
            )
        except ApiException as exc:
            results.append(
                {
                    "fileName": file.filename or f"uploaded-{index}.txt",
                    "status": "FAILED",
                    "errorCode": exc.code,
                }
            )
    return results


@router.post("/admin/documents/{doc_id}/versions", status_code=status.HTTP_202_ACCEPTED)
async def upload_document_version(
    doc_id: str,
    file: UploadFile = File(...),
    buildConfig: str = Form(default="{}"),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    actor: Actor = Depends(require_admin),
):
    doc = KnowledgeService.get_document(actor, doc_id)
    content = await file.read()
    try:
        cfg = json.loads(buildConfig) if buildConfig else {}
    except Exception as exc:
        raise ApiException(400, "INVALID_CONFIG", "buildConfig 必须是合法 JSON") from exc
    if not isinstance(cfg, dict):
        raise ApiException(400, "INVALID_CONFIG", "buildConfig 必须是 JSON 对象")
    return KnowledgeService.upload_document(
        actor=actor,
        kb_id=doc["kbId"],
        document_id=doc_id,
        filename=file.filename or "uploaded.txt",
        file_bytes=content,
        idempotency_key=idempotency_key,
        build_config=cfg,
    )


# Job polling endpoints
@router.get("/admin/jobs")
def list_jobs(actor: Actor = Depends(require_admin)):
    return KnowledgeService.list_jobs(actor)


@router.get("/admin/jobs/{job_id}")
def get_job(job_id: str, actor: Actor = Depends(require_admin)):
    return KnowledgeService.get_job(actor, job_id)


@router.post("/admin/jobs/{job_id}/retry")
def retry_job(job_id: str, actor: Actor = Depends(require_admin)):
    return KnowledgeService.control_job(actor, job_id, cancel=False)


@router.post("/admin/jobs/{job_id}/cancel")
def cancel_job(job_id: str, actor: Actor = Depends(require_admin)):
    return KnowledgeService.control_job(actor, job_id, cancel=True)


# Preview endpoint
@router.get("/documents/{doc_id}/versions/{version_id}/preview")
def get_preview(doc_id: str, version_id: str, actor: Actor = Depends(get_current_actor)):
    return KnowledgeService.get_preview(actor, doc_id, version_id)


@router.get("/documents/{doc_id}/artifacts/{artifact_id}")
def download_artifact(
    doc_id: str,
    artifact_id: str,
    actor: Actor = Depends(get_current_actor),
):
    data = KnowledgeService.get_artifact(actor, doc_id, artifact_id)
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": 'attachment; filename="source.bin"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )


@router.post("/admin/builds/{build_id}/accept-warnings")
def accept_build_warnings(
    build_id: str,
    body: Dict[str, Any],
    actor: Actor = Depends(require_admin),
):
    return KnowledgeService.accept_warnings(actor, build_id, body.get("reason", ""))


# Document ACL endpoints
@router.get("/admin/documents/{doc_id}/acl")
def get_document_acl(doc_id: str, actor: Actor = Depends(require_admin)):
    return KnowledgeService.get_document_acl(actor, doc_id)


@router.put("/admin/documents/{doc_id}/acl")
def update_document_acl(doc_id: str, payload: UpdateDocAclRequest, actor: Actor = Depends(require_admin)):
    return KnowledgeService.update_document_acl(actor, doc_id, payload.expectedVersion, payload.mode, payload.readers)


# Document Lifecycle endpoints (trash, restore, purge)
@router.post("/admin/documents/{doc_id}/{action}")
def document_lifecycle(doc_id: str, action: str, payload: LifecycleActionRequest, actor: Actor = Depends(require_admin)):
    return KnowledgeService.document_lifecycle(actor, doc_id, action, payload.expectedVersion)

