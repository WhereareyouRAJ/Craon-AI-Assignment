from prometheus_client import Counter, Gauge, Histogram

JOBS_CREATED = Counter(
    name="jobs_created_total",
    documentation="Total number of jobs created, by type",
    labelnames=["job_type"],
)

JOBS_FAILED = Counter(
    name="jobs_failed_total",
    documentation="Total number of jobs that failed permanently, by type",
    labelnames=["job_type"],
)

JOBS_SUCCEEDED = Counter(
    name="jobs_succeeded_total",
    documentation="Total number of jobs that completed successfully, by type",
    labelnames=["job_type"],
)

JOBS_BY_STATUS = Gauge(
    name="jobs_by_status",
    documentation="Current number of jobs in each status",
    labelnames=["status"],
)

JOB_DURATION = Histogram(
    name="job_duration_seconds",
    documentation="How long jobs take to complete, in seconds",
    labelnames=["job_type"],
    buckets=[1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 60.0],
)

def record_job_created(job_type: str):
    """Call this when a new job is created (in POST /jobs)."""
    JOBS_CREATED.labels(job_type=job_type).inc()          # increment by 1
    JOBS_BY_STATUS.labels(status="pending").inc()
 
 
def record_job_processing(job_type: str):
    """Call this when a worker picks up a job."""
    JOBS_BY_STATUS.labels(status="pending").dec()         # one less pending
    JOBS_BY_STATUS.labels(status="processing").inc()      # one more processing
 
 
def record_job_succeeded(job_type: str, duration_seconds: float):
    """Call this when a job finishes successfully."""
    JOBS_SUCCEEDED.labels(job_type=job_type).inc()
    JOBS_BY_STATUS.labels(status="processing").dec()
    JOBS_BY_STATUS.labels(status="done").inc()
    JOB_DURATION.labels(job_type=job_type).observe(duration_seconds)
 
 
def record_job_failed(job_type: str, duration_seconds: float):
    """Call this when a job fails permanently (all retries exhausted)."""
    JOBS_FAILED.labels(job_type=job_type).inc()
    JOBS_BY_STATUS.labels(status="processing").dec()
    JOBS_BY_STATUS.labels(status="failed").inc()
    JOB_DURATION.labels(job_type=job_type).observe(duration_seconds)
