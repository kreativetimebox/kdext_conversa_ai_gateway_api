"""Job polling endpoint — check async job status."""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.core.dependencies import verify_api_key_cached
from app.models.user import User
from app.models.tts import TextToSpeech
from app.models.stt import SpeechToText
from app.schemas.jobs import JobStatusResponse
from app.storage.audio_store import presign_audio_url

router = APIRouter(tags=["jobs"])


def _live_queue_position(db: Session, model, job) -> int | None:
    """Current position in the queue: queued jobs submitted at or before this one.

    The position stored at submit time goes stale as the queue drains, so it is
    recomputed here for jobs still queued.
    """
    if job.status != "queued":
        return None
    return (
        db.query(model)
        .filter(model.status == "queued", model.request_id <= job.request_id)
        .count()
    )


@router.get("/jobs/{job_id}", response_model=JobStatusResponse)
def get_job_status(job_id: int,
                   type: str | None = Query(default=None, pattern="^(tts|stt)$"),
                   user: User = Depends(verify_api_key_cached),
                   db: Session = Depends(get_db)):
    """Poll the status of an async TTS or STT job.

    Returns the current status and result (if completed).
    Users can only access their own jobs.

    Pass ?type=tts or ?type=stt to skip the lookup in the other job table —
    the DB is cross-region, so each extra query is a full network round-trip
    and this endpoint is polled in a tight loop.
    """
    # Try TTS first (unless the caller said the job is STT)
    tts_job = None
    if type != "stt":
        tts_job = (
            db.query(TextToSpeech)
            .filter(TextToSpeech.request_id == job_id, TextToSpeech.user_id == user.user_id)
            .first()
        )
    if tts_job:
        # Owner already verified via the user_id filter above; mint a short-lived
        # unguessable URL rather than exposing the raw enumerable storage path.
        return JobStatusResponse(
            job_id=tts_job.request_id,
            job_type="tts",
            status=tts_job.status,
            queue_position=_live_queue_position(db, TextToSpeech, tts_job),
            audio_url=presign_audio_url(tts_job.audio_url),
            download_url=presign_audio_url(
                tts_job.audio_url,
                download_name=f"conversa_tts_{tts_job.request_id}.{tts_job.format or 'wav'}",
            ),
            detail=tts_job.input_text,
            processing_time=tts_job.processing_time,
            error=tts_job.error_message,
            webhook_url=tts_job.webhook_url,
            webhook_sent_at=tts_job.webhook_sent_at,
            created_at=tts_job.created_at,
            updated_at=tts_job.completed_at,
        )

    # Try STT (unless the caller said the job is TTS)
    stt_job = None
    if type != "tts":
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
            queue_position=_live_queue_position(db, SpeechToText, stt_job),
            audio_url=presign_audio_url(stt_job.audio_url),
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
