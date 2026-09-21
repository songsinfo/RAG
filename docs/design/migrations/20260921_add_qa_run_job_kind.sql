-- Upgrade existing MySQL databases to permit durable QA_RUN jobs.
-- The original schema left the CHECK unnamed; MySQL typically generated job_chk_1.
-- If your server generated a different name, inspect information_schema.TABLE_CONSTRAINTS
-- and replace job_chk_1 before running this migration.
ALTER TABLE job DROP CHECK job_chk_1;
ALTER TABLE job
  ADD CONSTRAINT chk_job_kind
  CHECK (kind IN ('INGEST','PURGE','RESTORE','EVALUATE','EXPORT','CLEANUP','QA_RUN'));
