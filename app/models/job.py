"""
models/job.py — Defines what a "Job" looks like in MongoDB.

Beanie is an ODM (Object Document Mapper) — it lets us work with
MongoDB documents as Python objects instead of raw dictionaries.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from beanie import Document
from pydantic import Field


class JobStatus(str, Enum):
    """The four possible states a job can be in."""
    PENDING    = "pending"      # just created, not picked up yet
    PROCESSING = "processing"   # a Celery worker is running it
    DONE       = "done"         # finished successfully
    FAILED     = "failed"       # all retries exhausted, gave up


class JobType(str, Enum):
    """Supported job types — easy to extend later."""
    CSV_PARSE  = "csv_parse"    # simulates parsing a CSV file
    SEND_EMAIL = "send_email"   # simulates sending an email
    REPORT     = "report"       # simulates generating a report


class Job(Document):
    """  
    The MongoDB document for a job.
    Each field below becomes a column in the 'jobs' collection.
    """      

    # What kind of task this is
    job_type: JobType

    # Current lifecycle state
    status: JobStatus = JobStatus.PENDING

    # Arbitrary input data passed by the caller (e.g. CSV filename, email address)
    payload: dict = Field(default_factory=dict)

    # Result written back once the job finishes
    result: Optional[dict] = None

    # Error message if it failed
    error: Optional[str] = None

    # Timestamps — auto-set, never sent by the caller
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # How many times Celery has retried this job
    retry_count: int = 0

    # The Celery task ID — lets us cancel or inspect the task later
    celery_task_id: Optional[str] = None

    class Settings:
        name = "jobs"           # MongoDB collection name
        use_state_management = True  # lets Beanie track dirty fields


    def touch(self):
        """Call this before saving to keep updated_at fresh."""
        self.updated_at = datetime.now(timezone.utc)