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
        # 1. Resolve permitted active build IDs
        stmt = select(document_table.c.active_build_id).where(
            and_(
                document_table.c.tenant_id == actor.tenant_id,
                document_table.c.status == "LIVE",
                document_table.c.active_build_id.isnot(None),
            )
        )

        if kbIds:
            kb_list = [k.strip() for k in kbIds.split(",") if k.strip()]
            stmt = stmt.where(document_table.c.kb_id.in_(kb_list))

        # Check ACL for non-admin
        if not actor.admin:
            stmt = stmt.where(
                or_(
                    exists().where(
                        and_(
                            kb_grant_table.c.tenant_id == document_table.c.tenant_id,
                            kb_grant_table.c.kb_id == document_table.c.kb_id,
                            or_(
                                kb_grant_table.c.principal_type == "ALL",
                                kb_grant_table.c.user_id == actor.user_id,
                            ),
                        )
                    ),
                    exists().where(
                        and_(
                            document_grant_table.c.tenant_id == document_table.c.tenant_id,
                            document_grant_table.c.document_id == document_table.c.id,
                            document_grant_table.c.user_id == actor.user_id,
                        )
                    ),
                )
            )

        allowed_builds = [str(r.active_build_id) for r in conn.execute(stmt).fetchall()]

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
