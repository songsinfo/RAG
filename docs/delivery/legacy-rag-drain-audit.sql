-- READ-ONLY PRE-REMOVAL AUDIT, run against the target MySQL instance.
-- No UPDATE/DELETE is performed: absence of database access is not proof
-- that queues are empty. Preserve results as deployment approval evidence.
SELECT r.tenant_id, r.id AS run_id, r.mode, r.status AS run_status,
       j.id AS job_id, j.status AS job_status, j.lease_until,
       j.lease_epoch, j.attempt
  FROM qa_run r
  LEFT JOIN job j ON j.tenant_id = r.tenant_id
                 AND j.target_id = r.id AND j.kind = 'QA_RUN'
 WHERE r.mode = 'RAG'
   AND (r.status IN ('QUEUED','RUNNING','WAITING_APPROVAL')
        OR j.status IN ('QUEUED','RUNNING','RETRY','WAITING_APPROVAL'))
 ORDER BY r.tenant_id, r.created_at;

-- Snapshot of historical legacy modes, not a migration directive.
SELECT mode, status, COUNT(*) AS runs
  FROM qa_run GROUP BY mode, status ORDER BY mode, status;

-- Success gate: ZERO unresolved legacy runs plus verified recovery/cancel tests.
-- If rows remain, drain with the old compatible worker or explicitly terminate
-- through approved, audited existing application mechanisms BEFORE code removal.
-- Do not rewrite mode=RAG to AGENT or delete checkpoint/source history.
