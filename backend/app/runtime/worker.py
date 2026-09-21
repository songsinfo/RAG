import logging
import os
import socket
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from sqlalchemy import and_, or_, select, update

from app.db import get_db_connection
from app.knowledge.artifacts import read_artifact
from app.models import job_table

logger = logging.getLogger(__name__)


class DurableJobWorker:
    def __init__(self, worker_id: Optional[str] = None, lease_seconds: int = 120, poll_seconds: float = 1.0):
        self.worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}"
        self.lease_seconds = lease_seconds
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run_forever(self):
        logger.info("Durable worker started: %s", self.worker_id)
        while not self._stop.is_set():
            try:
                worked = self.run_once()
            except Exception:
                logger.exception("Durable worker iteration failed")
                worked = False
            if not worked:
                self._stop.wait(self.poll_seconds)

    def run_once(self) -> bool:
        job = self._claim_one()
        if not job:
            return False
        try:
            if job["kind"] == "INGEST":
                self._run_ingest(job)
            elif job["kind"] == "PURGE":
                self._run_purge(job)
            else:
                self._fail(job, "UNSUPPORTED_JOB_KIND")
        except Exception:
            logger.exception("Job %s failed", job["id"])
            self._fail(job, "WORKER_EXECUTION_FAILED")
        return True

    def _claim_one(self) -> Optional[Dict[str, Any]]:
        now = datetime.now(timezone.utc)
        with get_db_connection() as conn:
            row = conn.execute(
                select(job_table)
                .where(and_(
                    job_table.c.kind.in_(["INGEST", "PURGE"]),
                    job_table.c.available_at <= now,
                    or_(job_table.c.status == "QUEUED",
                        and_(job_table.c.status == "RUNNING", job_table.c.lease_until.isnot(None), job_table.c.lease_until < now)),
                ))
                .order_by(job_table.c.created_at.asc())
                .limit(1)
                .with_for_update(skip_locked=True)
            ).first()
            if not row:
                return None
            job = dict(row._mapping)
            conn.execute(
                update(job_table)
                .where(and_(job_table.c.tenant_id == job["tenant_id"], job_table.c.id == job["id"]))
                .values(status="RUNNING", lease_until=now + timedelta(seconds=self.lease_seconds),
                        lease_epoch=job_table.c.lease_epoch + 1, attempt=job_table.c.attempt + 1,
                        error_code=None, updated_at=now)
            )
            conn.commit()
            return job

    def _run_ingest(self, job: Dict[str, Any]):
        from app.knowledge.service import KnowledgeService
        cp = job.get("checkpoint") or {}
        data = read_artifact(str(cp["storageKey"]))
        KnowledgeService._execute_ingestion_job(
            str(job["tenant_id"]), str(job["id"]), str(cp["documentId"]),
            str(cp["versionId"]), str(cp["buildId"]), data, str(cp["filename"])
        )

    def _run_purge(self, job: Dict[str, Any]):
        from app.knowledge.service import KnowledgeService
        cp = job.get("checkpoint") or {}
        KnowledgeService._execute_purge(
            str(job["tenant_id"]), str(job["target_id"]), str(job["id"]), str(cp["deletionRecordId"])
        )

    def _fail(self, job: Dict[str, Any], code: str):
        with get_db_connection() as conn:
            conn.execute(
                update(job_table)
                .where(and_(job_table.c.tenant_id == job["tenant_id"], job_table.c.id == job["id"]))
                .values(status="FAILED", error_code=code, lease_until=None, updated_at=datetime.now(timezone.utc))
            )
            conn.commit()


def main():
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    DurableJobWorker().run_forever()


if __name__ == "__main__":
    main()
