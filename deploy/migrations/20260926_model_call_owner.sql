-- Existing MySQL databases: permit tenant-scoped Search calls without a QA run/job.
-- Fresh databases use the named constraint in docs/design/schema.sql.
-- Inspect SHOW CREATE TABLE model_call before applying this migration.
SET @old_owner_check = (
  SELECT tc.CONSTRAINT_NAME
  FROM information_schema.TABLE_CONSTRAINTS tc
  JOIN information_schema.CHECK_CONSTRAINTS cc
    ON cc.CONSTRAINT_SCHEMA = tc.CONSTRAINT_SCHEMA AND cc.CONSTRAINT_NAME = tc.CONSTRAINT_NAME
  WHERE tc.TABLE_SCHEMA = DATABASE() AND tc.TABLE_NAME = 'model_call'
    AND tc.CONSTRAINT_TYPE = 'CHECK'
    AND cc.CHECK_CLAUSE LIKE '%run_id%' AND cc.CHECK_CLAUSE LIKE '%job_id%'
  LIMIT 1
);
SET @alter_owner_check = CONCAT('ALTER TABLE model_call ',
  IF(@old_owner_check IS NULL, '',
    CONCAT('DROP CHECK `', REPLACE(@old_owner_check, '`', '``'), '`, ')),
  'ADD CONSTRAINT ck_model_call_owner CHECK (run_id IS NULL OR job_id IS NULL)');
PREPARE owner_statement FROM @alter_owner_check;
EXECUTE owner_statement;
DEALLOCATE PREPARE owner_statement;
