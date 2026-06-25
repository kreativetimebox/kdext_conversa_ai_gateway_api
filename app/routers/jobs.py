"""Job polling endpoint — check async job status."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.core.dependencies import verify_api_key
from app.models.user import User
from app.models.tts import TextToSpeech
from app.models.stt import SpeechToText
from app.schemas.jobs import JobStatusResponse

router = APIRouter(tags=["jobs"])


@router.get("/jobs/{job_id}", response_model=JobStatusResponse)
def get_job_status(job_id: int,
                   user: User = Depends(verify_api_key),
                   db: Session = Depends(get_db)):
    """Poll the status of an async TTS or STT job.

    Returns the current status and result (if completed).
    Users can only access their own jobs.
    """
    # Try TTS first
    tts_job = (
        db.query(TextToSpeech)
        .filter(TextToSpeech.request_id == job_id, TextToSpeech.user_id == user.user_id)
        .first()
    )
    if tts_job:
        return JobStatusResponse(
            job_id=tts_job.request_id,
            job_type="tts",
            status=tts_job.status,
            queue_position=tts_job.queue_position,
            audio_url=tts_job.audio_url,
            detail=tts_job.input_text,
            processing_time=tts_job.processing_time,
            error=tts_job.error_message,
            webhook_url=tts_job.webhook_url,
            webhook_sent_at=tts_job.webhook_sent_at,
            created_at=tts_job.created_at,
            updated_at=tts_job.completed_at,
        )

    # Try STT
    stt_job = (
        db.query(SpeechToText)
        .filter(SpeechToText.request_id == job_id, SpeechToText.user_id == user.user_id)
        .first()
    )
    if stt_job:
        return JobStatusResponse(
            job_id=stt_job.request_id,
            job_type="stt",
            status=stt_job.status,
            queue_position=stt_job.queue_position,
            audio_url=stt_job.audio_url,
            detail=stt_job.transcript,
            processing_time=stt_job.processing_time,
            error=stt_job.error_message,
            webhook_url=stt_job.webhook_url,
            webhook_sent_at=stt_job.webhook_sent_at,
            created_at=stt_job.created_at,
            updated_at=stt_job.completed_at,
            detected_language=stt_job.detected_language,
            segments=stt_job.segments,
        )


    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Job not found or does not belong to this user",
    )
