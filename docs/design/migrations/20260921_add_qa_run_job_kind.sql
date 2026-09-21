-- Upgrade older MySQL schemas to accept durable QA_RUN jobs.
-- Older installations normally expose the unnamed check as job_chk_1.
ALTER TABLE job DROP CHECK job_chk_1;
ALTER TABLE job ADD CONSTRAINT chk_job_kind
  CHECK (kind IN ('INGEST','PURGE','RESTORE','EVALUATE','EXPORT','CLEANUP','QA_RUN'));
