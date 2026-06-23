import time
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException, status
from sqlalchemy.orm import Session
from app.database import get_db
from app.core.dependencies import verify_api_key
from app.config import get_settings
from app.models.user import User
from app.models.stt import SpeechToText
from app.services.stt_service import transcribe
from app.storage.audio_store import save_audio
from app.services.usage import increment_success, increment_failure

router = APIRouter(tags=["stt"])
settings = get_settings()


@router.post("/speech-to-text")
async def speech_to_text(file: UploadFile = File(...),
                         language: str | None = Form(default=None),
                         webhook_url: str | None = Form(default=None),
                         user: User = Depends(verify_api_key),
                         db: Session = Depends(get_db)):
    data = await file.read()
    if len(data) > settings.max_audio_upload_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Uploaded audio file is too large"
        )
    content_type = file.content_type or "application/octet-stream"
    if content_type not in settings.allowed_audio_content_types:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Unsupported audio media type"
        )

    # Save incoming audio file (needed for both sync and async)
    filename = file.filename or "upload.wav"

    # ── Async mode: upload audio to S3, queue the job ──
    if settings.use_async_queue:
        from app.services.sqs_client import send_job

        # Compute queue position (number of currently queued jobs + 1)
        queue_pos = db.query(SpeechToText).filter(SpeechToText.status == "queued").count() + 1

        job = SpeechToText(
            audio="",
            user_id=user.user_id,
            status="queued",
            language=language,
            webhook_url=webhook_url,
            queue_position=queue_pos,
        )
        db.add(job)
        db.commit()
        db.refresh(job)

        # Upload audio to S3 so the worker can access it
        audio_url = save_audio(f"stt/{job.request_id}_{filename}", data)
        job.audio = audio_url
        db.commit()

        send_job(
            queue_url=settings.aws_sqs_stt_queue_url,
            job_id=job.request_id,
            job_type="stt",
            payload={
                "audio_url": audio_url,
                "filename": filename,
                "content_type": content_type,
                "language": language,
                "user_id": user.user_id,
                "webhook_url": webhook_url,
            },
        )
        return {
            "job_id": job.request_id,
            "status": "queued",
            "queue_position": queue_pos,
            "message": "Job submitted. Poll GET /jobs/{job_id} for status.",
        }

    # ── Sync mode: process immediately (original behavior) ──
    job = SpeechToText(audio="", user_id=user.user_id)
    db.add(job)
    db.commit()
    db.refresh(job)

    audio_url = save_audio(f"stt/{job.request_id}_{filename}", data)
    job.audio = audio_url
    db.commit()

    start = time.perf_counter()
    try:
        text = await transcribe(data, filename=filename, content_type=content_type, language=language)
        job.detail = text
        job.processing_time = round(time.perf_counter() - start, 3)
        job.updating_time = datetime.now(timezone.utc)
        increment_success(user.user_id, db)
        db.commit()
        return {
            "request_id": job.request_id,
            "detail": text,
            "audio_url": audio_url,
            "processing_time": job.processing_time,
            "current_time": job.current_time,
        }
    except Exception as exc:
        increment_failure(user.user_id, db)
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Engine failure: {str(exc)}"
        )
