from typing import Optional, List
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, and_, or_, exists

from app.db import get_db_connection
from app.identity.deps import Actor, get_current_actor
from app.models import document_table, kb_grant_table, document_grant_table
from app.retrieval.milvus_service import MilvusHybridService

router = APIRouter(tags=["retrieval"])


@router.get("/search")
def search(
    query: str = Query(..., min_length=1),
    kbIds: Optional[str] = Query(None),
    actor: Actor = Depends(get_current_actor),
):
    with get_db_connection() as conn:
        from app.identity.deps import get_readable_builds
        kb_list = [k.strip() for k in kbIds.split(",") if k.strip()] if kbIds else None
        allowed_builds = get_readable_builds(conn, actor, kb_list)

    # 2. Execute hybrid search
    hits = MilvusHybridService.search(
        tenant_id=actor.tenant_id,
        query=query,
        allowed_build_ids=allowed_builds,
        top_k=10,
    )

    evidence = [
        {
            "evidenceId": f"ev-{h['chunkId']}",
            "chunkId": h["chunkId"],
            "documentId": h["documentId"],
            "text": h["text"],
            "score": round(h["score"], 6),
        }
        for h in hits
    ]

    return {
        "query": query,
        "evidence": evidence,
    }
