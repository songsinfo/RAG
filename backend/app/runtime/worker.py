import logging
import os
import socket
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from sqlalchemy import and_, or_, select, update

from app.db import get_db_connection
from app.knowledge.artifacts import read_artifact
from app.models import app_user_table, deletion_record_table, job_table, qa_run_table
from app.qa.runtime import is_retryable_job_code
from app.qa.model_usage import usage_scope

logger = logging.getLogger(__name__)


class DurableJobWorker:
    def __init__(
        self,
        worker_id: Optional[str] = None,
        lease_seconds: int = 120,
        poll_seconds: float = 1.0,
        max_attempts: int = 3,
        kinds: Optional[set[str]] = None,
    ):
        self.worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}"
        self.lease_seconds = lease_seconds
        self.poll_seconds = poll_seconds
        self.max_attempts = max_attempts
        self.kinds = kinds or {"INGEST", "PURGE", "EVALUATE", "QA_RUN"}
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
        heartbeat_stop = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            args=(job, heartbeat_stop),
            name=f"job-heartbeat-{job['id']}",
            daemon=True,
        )
        heartbeat.start()
        try:
            if job["kind"] == "INGEST":
                self._run_ingest(job)
            elif job["kind"] == "PURGE":
                self._run_purge(job)
            elif job["kind"] == "EVALUATE":
                self._run_evaluate(job)
            elif job["kind"] == "QA_RUN":
                self._run_qa(job)
            else:
                self._fail(job, "UNSUPPORTED_JOB_KIND")
        except Exception:
            logger.exception("Job %s failed", job["id"])
            self._fail(job, "WORKER_EXECUTION_FAILED")
        finally:
            heartbeat_stop.set()
            heartbeat.join(timeout=2)
        self._requeue_failed_if_retryable(job)
        return True

    def _claim_one(self) -> Optional[Dict[str, Any]]:
        now = datetime.now(timezone.utc)
        with get_db_connection() as conn:
            row = conn.execute(
                select(job_table)
                .where(and_(
                    job_table.c.kind.in_(sorted(self.kinds)),
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
            next_epoch = int(job.get("lease_epoch") or 0) + 1
            conn.execute(
                update(job_table)
                .where(and_(job_table.c.tenant_id == job["tenant_id"], job_table.c.id == job["id"]))
                .values(status="RUNNING", lease_until=now + timedelta(seconds=self.lease_seconds),
                        lease_epoch=next_epoch, attempt=job_table.c.attempt + 1,
                        error_code=None, updated_at=now)
            )
            conn.commit()
            job["lease_epoch"] = next_epoch
            return job

    def _heartbeat_loop(self, job: Dict[str, Any], stop: threading.Event):
        interval = max(5.0, self.lease_seconds / 3.0)
        while not stop.wait(interval):
            now = datetime.now(timezone.utc)
            with get_db_connection() as conn:
                result = conn.execute(
                    update(job_table)
                    .where(and_(
                        job_table.c.tenant_id == job["tenant_id"],
                        job_table.c.id == job["id"],
                        job_table.c.status == "RUNNING",
                        job_table.c.lease_epoch == job["lease_epoch"],
                    ))
                    .values(
                        lease_until=now + timedelta(seconds=self.lease_seconds),
                        updated_at=now,
                    )
                )
                conn.commit()
                if result.rowcount == 0:
                    logger.warning("Lease lost for job %s; heartbeat stopped", job["id"])
                    return

    def _run_ingest(self, job: Dict[str, Any]):
        from app.knowledge.service import KnowledgeService
        cp = job.get("checkpoint") or {}
        data = read_artifact(str(cp["storageKey"]))
        with usage_scope(str(job["tenant_id"]), job_id=str(job["id"])):
            KnowledgeService._execute_ingestion_job(
                str(job["tenant_id"]), str(job["id"]), str(cp["documentId"]),
                str(cp["versionId"]), str(cp["buildId"]), data, str(cp["filename"]),
                lease_epoch=int(job["lease_epoch"]),
            )

    def _run_purge(self, job: Dict[str, Any]):
        from app.knowledge.service import KnowledgeService
        cp = job.get("checkpoint") or {}
        KnowledgeService._execute_purge(
            str(job["tenant_id"]), str(job["target_id"]), str(job["id"]), str(cp["deletionRecordId"]),
            lease_epoch=int(job["lease_epoch"]),
        )

    def _run_evaluate(self, job: Dict[str, Any]):
        from app.evaluation.runner import EvaluationRunner
        from app.identity.deps import Actor

        with get_db_connection() as conn:
            user = conn.execute(
                select(
                    app_user_table.c.id,
                    app_user_table.c.is_admin,
                    app_user_table.c.status,
                ).where(and_(
                    app_user_table.c.tenant_id == job["tenant_id"],
                    app_user_table.c.id == job["requested_by"],
                ))
            ).first()
        if not user or str(user.status) != "ACTIVE":
            self._fail(job, "REQUESTER_NOT_ACTIVE")
            return

        actor = Actor(
            tenant_id=str(job["tenant_id"]),
            user_id=str(user.id),
            admin=bool(user.is_admin),
            security_epoch=0,
        )
        EvaluationRunner._execute_run(actor, str(job["target_id"]))

    def _run_qa(self, job: Dict[str, Any]):
        from app.identity.deps import Actor
        from app.qa.service import QaService

        with get_db_connection() as conn:
            user = conn.execute(
                select(
                    app_user_table.c.id,
                    app_user_table.c.is_admin,
                    app_user_table.c.status,
                ).where(and_(
                    app_user_table.c.tenant_id == job["tenant_id"],
                    app_user_table.c.id == job["requested_by"],
                ))
            ).first()
        if not user or str(user.status) != "ACTIVE":
            self._fail(job, "REQUESTER_NOT_ACTIVE")
            return

        actor = Actor(
            tenant_id=str(job["tenant_id"]),
            user_id=str(user.id),
            admin=bool(user.is_admin),
            security_epoch=0,
        )
        QaService.execute_queued_run(
            actor,
            str(job["target_id"]),
            job_id=str(job["id"]),
            lease_epoch=int(job["lease_epoch"]),
        )

    def _requeue_failed_if_retryable(self, job: Dict[str, Any]):
        with get_db_connection() as conn:
            row = conn.execute(
                select(
                    job_table.c.status,
                    job_table.c.attempt,
                    job_table.c.lease_epoch,
                    job_table.c.error_code,
                ).where(and_(
                    job_table.c.tenant_id == job["tenant_id"],
                    job_table.c.id == job["id"],
                ))
            ).first()
            if not row or str(row.status) != "FAILED":
                return
            if int(row.attempt or 0) >= self.max_attempts:
                return
            if not is_retryable_job_code(str(row.error_code or "")):
                return
            if int(row.lease_epoch or 0) != int(job.get("lease_epoch") or 0):
                return
            delay_seconds = min(60, 2 ** max(0, int(row.attempt or 1) - 1))
            now = datetime.now(timezone.utc)
            conn.execute(
                update(job_table)
                .where(and_(
                    job_table.c.tenant_id == job["tenant_id"],
                    job_table.c.id == job["id"],
                    job_table.c.status == "FAILED",
                    job_table.c.lease_epoch == job["lease_epoch"],
                ))
                .values(
                    status="QUEUED",
                    available_at=now + timedelta(seconds=delay_seconds),
                    lease_until=None,
                    updated_at=now,
                )
            )
            if job.get("kind") == "QA_RUN":
                conn.execute(
                    update(qa_run_table)
                    .where(and_(
                        qa_run_table.c.tenant_id == job["tenant_id"],
                        qa_run_table.c.id == job["target_id"],
                        qa_run_table.c.status == "FAILED",
                    ))
                    .values(
                        status="QUEUED",
                        stage="ROUTING",
                        terminal_reason=None,
                        finished_at=None,
                        updated_at=now,
                    )
                )
            conn.commit()

    def _fail(self, job: Dict[str, Any], code: str):
        now = datetime.now(timezone.utc)
        with get_db_connection() as conn:
            conn.execute(
                update(job_table)
                .where(and_(
                    job_table.c.tenant_id == job["tenant_id"],
                    job_table.c.id == job["id"],
                    job_table.c.lease_epoch == job.get("lease_epoch"),
                ))
                .values(status="FAILED", error_code=code, lease_until=None, updated_at=now)
            )
            if job.get("kind") == "PURGE":
                deletion_record_id = (job.get("checkpoint") or {}).get("deletionRecordId")
                if deletion_record_id:
                    conn.execute(
                        update(deletion_record_table)
                        .where(and_(
                            deletion_record_table.c.tenant_id == job["tenant_id"],
                            deletion_record_table.c.id == deletion_record_id,
                        ))
                        .values(status="FAILED", updated_at=now)
                    )
            conn.commit()


def main():
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    DurableJobWorker().run_forever()


if __name__ == "__main__":
    main()
