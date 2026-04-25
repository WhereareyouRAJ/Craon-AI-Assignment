"""
routes/jobs.py — The three API endpoints.

POST /jobs        → create a job (rate-limited to 10/min per IP)
GET  /jobs/:id    → get one job's status
GET  /jobs        → paginated list of jobs
"""

from typing import Optional
from fastapi import APIRouter, HTTPException, Request, Query
from pydantic import BaseModel
from beanie import PydanticObjectId

from app.models.job import Job, JobStatus, JobType
from app.workers.celery_app import process_job

router = APIRouter(prefix="/jobs", tags=["Jobs"])


# ── Request / Response schemas ─────────────────────────────────────────────────

class CreateJobRequest(BaseModel):
    """What the caller must send to POST /jobs."""
    job_type: JobType
    payload:  dict = {}

    class Config:
        json_schema_extra = {
            "examples": [
                {
                    "job_type": "csv_parse",
                    "payload": {"filename": "sales_q1.csv", "rows": 1500}
                },
                {
                    "job_type": "send_email",
                    "payload": {"to": "alice@example.com", "subject": "Your report is ready"}
                },
                {
                    "job_type": "report",
                    "payload": {"name": "monthly_revenue"}
                },
            ]
        }


class JobResponse(BaseModel):
    """What we return for a single job — hides internal MongoDB fields."""
    id:             str
    job_type:       JobType
    status:         JobStatus
    payload:        dict
    result:         Optional[dict]  = None
    error:          Optional[str]   = None
    retry_count:    int
    celery_task_id: Optional[str]   = None
    created_at:     str
    updated_at:     str

    @classmethod
    def from_job(cls, job: Job) -> "JobResponse":
        return cls(
            id=str(job.id),
            job_type=job.job_type,
            status=job.status,
            payload=job.payload,
            result=job.result,
            error=job.error,
            retry_count=job.retry_count,
            celery_task_id=job.celery_task_id,
            created_at=job.created_at.isoformat(),
            updated_at=job.updated_at.isoformat(),
        )


class PaginatedJobsResponse(BaseModel):
    """Wraps a page of jobs with pagination metadata."""
    items:      list[JobResponse]
    total:      int
    page:       int
    page_size:  int
    total_pages: int


# ── Endpoint 1: Create a job ───────────────────────────────────────────────────

@router.post(
    "",
    response_model=JobResponse,
    status_code=202,    # 202 Accepted = "we got it, working on it"
    summary="Enqueue a new background job",
)
async def create_job(body: CreateJobRequest, request: Request):
    """
    Creates a job record in MongoDB (status=pending), then sends it
    to Celery via Redis.  Returns immediately — the work runs in the background.

    Rate limit: 10 requests per minute per IP address.
    """
    # 1. Save the job to MongoDB so we have a trackable ID immediately
    job = Job(job_type=body.job_type, payload=body.payload)
    await job.insert()

    # 2. Hand off to Celery — this is non-blocking (just puts a message in Redis)
    task = process_job.delay(
        job_id=str(job.id),
        job_type=body.job_type.value,
        payload=body.payload,
    )

    # 3. Save the Celery task ID so we can reference it later if needed
    job.celery_task_id = task.id
    await job.save()

    return JobResponse.from_job(job)


# ── Endpoint 2: Get a single job ───────────────────────────────────────────────

@router.get(
    "/{job_id}",
    response_model=JobResponse,
    summary="Check the status of a job",
)
async def get_job(job_id: str):
    """
    Fetches a single job by its MongoDB ObjectId.
    Poll this endpoint to track progress: pending → processing → done / failed.
    """
    # Validate the ID format before hitting the DB
    try:
        oid = PydanticObjectId(job_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid job ID format")

    job = await Job.get(oid)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    return JobResponse.from_job(job)


# ── Endpoint 3: List jobs (paginated) ─────────────────────────────────────────

@router.get(
    "",
    response_model=PaginatedJobsResponse,
    summary="List all jobs with optional filtering",
)
async def list_jobs(
    page:      int            = Query(1,    ge=1,  description="Page number (starts at 1)"),
    page_size: int            = Query(10,   ge=1,  le=100, description="Jobs per page (max 100)"),
    status:    Optional[JobStatus] = Query(None,   description="Filter by status"),
    job_type:  Optional[JobType]   = Query(None,   description="Filter by job type"),
):
    """
    Returns a paginated list of jobs, newest first.
    Optionally filter by status or job_type.
    """
    # Build the filter dict — only add fields the caller actually specified
    filters = {}
    if status:
        filters["status"]   = status.value
    if job_type:
        filters["job_type"] = job_type.value

    # Count total matching documents (for pagination metadata)
    total = await Job.find(filters).count()

    # Fetch the right page, sorted newest first
    skip = (page - 1) * page_size
    jobs = await (
        Job.find(filters)
        .sort("-created_at")        # minus = descending
        .skip(skip)
        .limit(page_size)
        .to_list()
    )

    return PaginatedJobsResponse(
        items=[JobResponse.from_job(j) for j in jobs],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=max(1, -(-total // page_size)),   # ceiling division
    )