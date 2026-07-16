"""Job status schemas for async queue responses."""

from datetime import datetime
from pydantic import BaseModel


class JobSubmitResponse(BaseModel):
    """Returned when a job is submitted to the async queue (HTTP 202)."""

    job_id: int
    job_type: str   # "tts" or "stt"
    status: str     # "queued"
    queue_position: int | None = None
    message: str = "Job submitted. Poll GET /jobs/{job_id} for status."


class JobStatusResponse(BaseModel):
    """Returned by GET /jobs/{job_id}."""

    job_id: int
    job_type: str           # "tts" or "stt"
    status: str             # "queued" | "processing" | "completed" | "failed"
    queue_position: int | None = None
    audio_url: str | None = None
    # Same audio, but presigned to download-with-filename (Content-Disposition)
    # instead of playing inline. Lets the client save the file without a new tab.
    download_url: str | None = None
    detail: str | None = None
    processing_time: float | None = None
    error: str | None = None
    webhook_url: str | None = None
    webhook_sent_at: datetime | None = None
    created_at: datetime
    updated_at: datetime | None = None
    detected_language: str | None = None
    segments: list | None = None

