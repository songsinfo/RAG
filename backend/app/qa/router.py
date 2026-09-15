from typing import Dict, Any, Optional
from fastapi import APIRouter, Depends, Header, Response, status
from app.identity.deps import Actor, get_current_actor
from app.qa.service import QaService

router = APIRouter(tags=["qa"])


@router.post("/conversations", status_code=status.HTTP_201_CREATED)
def create_conversation(
    body: Dict[str, Any] = {},
    actor: Actor = Depends(get_current_actor),
):
    return QaService.create_conversation(actor, body)


@router.post("/sessions", status_code=status.HTTP_201_CREATED)
def create_session_alias(
    body: Dict[str, Any] = {},
    actor: Actor = Depends(get_current_actor),
):
    """Compatibility alias for contract tests calling /api/v1/sessions."""
    return QaService.create_conversation(actor, body)


@router.get("/conversations")
def list_conversations(
    actor: Actor = Depends(get_current_actor),
):
    return QaService.list_conversations(actor)


@router.get("/conversations/{conversation_id}")
def get_conversation_detail(
    conversation_id: str,
    actor: Actor = Depends(get_current_actor),
):
    return QaService.get_conversation_detail(actor, conversation_id)


@router.delete("/conversations/{conversation_id}")
def delete_conversation(
    conversation_id: str,
    actor: Actor = Depends(get_current_actor),
):
    QaService.delete_conversation(actor, conversation_id)
    return {"status": "DELETED"}


@router.post("/conversations/{conversation_id}/segments")
def create_segment(
    conversation_id: str,
    body: Dict[str, Any] = {},
    actor: Actor = Depends(get_current_actor),
):
    return QaService.create_segment(actor, conversation_id, body)


@router.post("/conversations/{conversation_id}/runs", status_code=status.HTTP_202_ACCEPTED)
def submit_run(
    conversation_id: str,
    body: Dict[str, Any],
    response: Response,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    actor: Actor = Depends(get_current_actor),
):
    response.status_code = status.HTTP_202_ACCEPTED
    return QaService.submit_run(actor, conversation_id, body, idempotency_key)


@router.get("/runs/{run_id}")
def get_run(
    run_id: str,
    actor: Actor = Depends(get_current_actor),
):
    return QaService.get_run(actor, run_id)


@router.post("/runs/{run_id}/cancel")
def cancel_run(
    run_id: str,
    actor: Actor = Depends(get_current_actor),
):
    return QaService.cancel_run(actor, run_id)


@router.get("/runs/{run_id}/evidence/{evidence_id}")
def get_evidence(
    run_id: str,
    evidence_id: str,
    actor: Actor = Depends(get_current_actor),
):
    return QaService.get_evidence(actor, run_id, evidence_id)


@router.get("/admin/runs/{run_id}/trace")
def get_trace(
    run_id: str,
    actor: Actor = Depends(get_current_actor),
):
    return QaService.get_trace(actor, run_id)


@router.put("/runs/{run_id}/feedback")
def submit_feedback(
    run_id: str,
    body: Dict[str, Any],
    actor: Actor = Depends(get_current_actor),
):
    return QaService.submit_feedback(actor, run_id, body)


@router.get("/admin/feedback")
def admin_feedback_list(
    actor: Actor = Depends(get_current_actor),
):
    return QaService.admin_feedback_list(actor)


@router.get("/admin/feedback/{feedback_id}")
def admin_feedback_detail(
    feedback_id: str,
    actor: Actor = Depends(get_current_actor),
):
    return QaService.admin_feedback_detail(actor, feedback_id)


@router.patch("/admin/feedback/{feedback_id}")
def admin_feedback_update(
    feedback_id: str,
    body: Dict[str, Any],
    actor: Actor = Depends(get_current_actor),
):
    return QaService.admin_feedback_update(actor, feedback_id, body)

