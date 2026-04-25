"""
workers/celery_app.py — Celery configuration AND task definitions.

Celery is a "task queue": your FastAPI app drops a message into Redis,
and a separate Celery worker process picks it up and runs the actual work.
This keeps your API fast and non-blocking.
"""

import asyncio
import random
import time
from celery import Celery
from celery.utils.log import get_task_logger

from app.config import settings

logger = get_task_logger(__name__)

# ── Celery instance ────────────────────────────────────────────────────────────
# broker  = where tasks are SENT (Redis queue)
# backend = where RESULTS are stored (Redis again, but a different key namespace)
celery_app = Celery(
    "job_queue",
    broker=settings.redis_url,
    backend=settings.redis_url,
)

celery_app.conf.update(
    # Serialise messages as JSON (human-readable, safe)
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],

    # Results expire after 24 h so Redis doesn't fill up
    result_expires=86_400,

    # Timezone
    timezone="UTC",
    enable_utc=True,

    # Worker pulls one task at a time — fair distribution across workers
    worker_prefetch_multiplier=1,
    task_acks_late=True,        # only ack (remove from queue) AFTER task finishes
)


# ── Helper: run an async function from sync Celery task ───────────────────────
def run_async(coro):
    """Celery tasks are sync; use this to call async MongoDB helpers."""
    return asyncio.run(coro)


# ── Helper: update job state in MongoDB ───────────────────────────────────────
async def _update_job(job_id: str, **fields):
    """
    Opens a fresh Motor connection just for this update.
    We can't reuse FastAPI's connection because Celery runs in a
    different process with no running event loop.
    """
    from motor.motor_asyncio import AsyncIOMotorClient
    from beanie import init_beanie
    from app.models.job import Job
    from datetime import datetime, timezone

    client = AsyncIOMotorClient(settings.mongodb_url)
    await init_beanie(database=client[settings.mongodb_db_name], document_models=[Job])

    job = await Job.get(job_id)
    if job:
        for key, val in fields.items():
            setattr(job, key, val)
        job.updated_at = datetime.now(timezone.utc)
        await job.save()

    client.close()


# ── Simulated work functions ───────────────────────────────────────────────────
def _simulate_csv_parse(payload: dict) -> dict:
    """
    Pretend we're reading a large CSV row by row.
    Sleeps to mimic I/O time.
    """
    filename = payload.get("filename", "data.csv")
    num_rows = payload.get("rows", random.randint(500, 2000))
    logger.info(f"Parsing CSV: {filename} ({num_rows} rows)")

    time.sleep(3)   # simulate reading the file

    # Randomly fail 20% of the time so we can see retry behaviour
    if random.random() < 0.2:
        raise ValueError(f"Corrupt data found in {filename} at row {random.randint(1, num_rows)}")

    return {"filename": filename, "rows_processed": num_rows, "status": "parsed"}


def _simulate_send_email(payload: dict) -> dict:
    """Pretend we're calling an SMTP server."""
    recipient = payload.get("to", "user@example.com")
    subject   = payload.get("subject", "Hello!")
    logger.info(f"Sending email to {recipient}: {subject}")

    time.sleep(2)   # simulate SMTP round-trip

    if random.random() < 0.15:
        raise ConnectionError(f"SMTP server rejected connection for {recipient}")

    return {"to": recipient, "subject": subject, "message_id": f"msg_{random.randint(10000,99999)}"}


def _simulate_report(payload: dict) -> dict:
    """Pretend we're aggregating data and rendering a PDF."""
    report_name = payload.get("name", "monthly_report")
    logger.info(f"Generating report: {report_name}")

    time.sleep(5)   # reports take longer

    return {"report": report_name, "pages": random.randint(5, 40), "format": "pdf"}


# ── The actual Celery task ─────────────────────────────────────────────────────
@celery_app.task(
    bind=True,                      # gives us `self` so we can call self.retry()
    max_retries=3,                  # 3 retries before marking as FAILED
    default_retry_delay=5,          # base delay in seconds (grows exponentially)
    name="workers.process_job",
)
def process_job(self, job_id: str, job_type: str, payload: dict):
    """
    Main Celery task.  Runs inside the Celery worker process.

    Retry policy:
      attempt 1  fails → wait  5s  → attempt 2
      attempt 2  fails → wait 10s  → attempt 3
      attempt 3  fails → wait 20s  → mark FAILED
    (delay doubles each time: exponential backoff)
    """
    logger.info(f"[{job_id}] Starting {job_type} (attempt {self.request.retries + 1})")

    # ── Mark as PROCESSING ──────────────────────────────────────────────────
    run_async(_update_job(
        job_id,
        status="processing",
        celery_task_id=self.request.id,
        retry_count=self.request.retries,
    ))

    try:
        # ── Do the actual (simulated) work ──────────────────────────────────
        if job_type == "csv_parse":
            result = _simulate_csv_parse(payload)
        elif job_type == "send_email":
            result = _simulate_send_email(payload)
        elif job_type == "report":
            result = _simulate_report(payload)
        else:
            raise ValueError(f"Unknown job_type: {job_type}")

        # ── Success → mark DONE ─────────────────────────────────────────────
        run_async(_update_job(job_id, status="done", result=result))
        logger.info(f"[{job_id}] Completed successfully")
        return result

    except Exception as exc:
        retry_num = self.request.retries          # 0-based count of retries so far
        max_ret   = self.max_retries

        if retry_num < max_ret:
            # Exponential backoff: 5s, 10s, 20s …
            wait = self.default_retry_delay * (2 ** retry_num)
            logger.warning(f"[{job_id}] Failed (attempt {retry_num + 1}/{max_ret + 1}). "
                           f"Retrying in {wait}s. Error: {exc}")

            run_async(_update_job(
                job_id,
                status="processing",          # still processing, just retrying
                retry_count=retry_num + 1,
                error=str(exc),               # show last error while retrying
            ))

            # self.retry() raises a special Celery exception that reschedules the task
            raise self.retry(exc=exc, countdown=wait)
        else:
            # All retries exhausted → mark FAILED permanently
            logger.error(f"[{job_id}] All {max_ret + 1} attempts failed. Giving up. Error: {exc}")
            run_async(_update_job(
                job_id,
                status="failed",
                error=str(exc),
                retry_count=retry_num,
            ))
            # Don't re-raise — Celery will mark the task as FAILURE either way