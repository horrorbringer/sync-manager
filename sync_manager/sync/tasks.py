from celery import shared_task
from flask import current_app

from .. import db
from ..audit import record_audit
from ..models import SyncJob
from ..notifications.service import notify_async, sync_message
from .engine import synchronize


def execute_sync_job(job_id):
    job = db.session.get(SyncJob, job_id)
    if job is None:
        current_app.logger.error("Queued synchronization job %s no longer exists", job_id)
        return
    if job.status != "pending":
        current_app.logger.info(
            "Skipping synchronization job %s because its status is %s",
            job_id,
            job.status,
        )
        return
    try:
        synchronize(job, batch_size=current_app.config["SYNC_BATCH_SIZE"])
        record_audit("sync.completed", "job={}".format(job.id), user_id=job.initiated_by_id)
        notify_async("sync_success", sync_message(job, successful=True))
    except Exception as exc:
        record_audit(
            "sync.failed",
            "job={} error={}".format(job.id, exc),
            user_id=job.initiated_by_id,
        )
        notify_async("sync_failure", sync_message(job, successful=False, error=exc))
        current_app.logger.exception("Synchronization job %s failed", job.id)
        raise


@shared_task(name="sync_manager.run_sync_job")
def run_sync_job(job_id):
    try:
        return execute_sync_job(job_id)
    finally:
        db.session.remove()


def enqueue_job(job_id):
    if current_app.config["SYNC_EXECUTION_MODE"] == "inline":
        return execute_sync_job(job_id)

    celery_app = run_sync_job.app
    try:
        workers = celery_app.control.inspect(timeout=1).ping()
    except Exception as exc:
        raise ConnectionError("Unable to contact the Celery worker: {}".format(exc)) from exc
    if not workers:
        raise ConnectionError(
            "No Celery worker is available. Start the worker or use SYNC_EXECUTION_MODE=inline."
        )
    return run_sync_job.delay(job_id)
