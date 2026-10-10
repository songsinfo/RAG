# Legacy RAG execution shrink - preflight (NOT YET APPROVED)

PR #14 only routes **new** queries to Agent. Legacy RAG executions must not
silently be resumed as Agent or have their historical results rewritten.

1. Run [read-only inventory](legacy-rag-drain-audit.sql) in every production
   tenant/region after backing up qa_run, job and checkpoints. Attach redacted
   query output, environment, timestamp and responsible approver to the rollout.
2. Stop new legacy submissions (new API already normalizes to AGENT). Ensure the
   **old compatible worker** drains QUEUED/RUNNING RAG jobs; if a run is orphaned,
   explicitly record error and terminalize through the approved operational
   procedure. Verify that cancellation, retries and HITL are accounted for.
3. Only with **zero unresolved rows** and verified recovery, strict Judge,
   document ACL, audit and read-only historic viewing can the obsolete Python
   RAG execution branch be removed. Preserve RetrievalPipeline, /search,
   RETRIEVAL_* evaluations and historical mode parsing.
4. Roll back by redeploying compatible workers before re-enabling any old
   pending job processing. Do not roll back schema to NOT NULL document_id
   nor drop TOOL/segment snapshots.

**Status:** Awaiting target-environment database evidence. The historical RAG
branch is deliberately retained pending this safety gate; T07/UA-09 remains
blocked rather than falsely marked complete.
