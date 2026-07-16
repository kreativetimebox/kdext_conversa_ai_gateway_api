import asyncio
import time
import uuid
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException, status
from sqlalchemy.orm import Session
from app.database import get_db
from app.core.dependencies import verify_api_key_cached
from app.config import get_settings
from app.models.user import User
from app.models.stt import SpeechToText
from app.services.stt_service import transcribe
from app.storage.audio_store import save_audio, presign_audio_url
from app.services.usage import increment_success, increment_failure
from app.services.rate_limiter import check_rate_limit

router = APIRouter(tags=["stt"])
settings = get_settings()


@router.post("/speech-to-text")
async def speech_to_text(file: UploadFile = File(...),
                         language: str | None = Form(default=None),
                         webhook_url: str | None = Form(default=None),
                         user: User = Depends(verify_api_key_cached),
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

    # Rate-limit before creating any job row — applies to sync AND async mode.
    check_rate_limit(user.user_id, "stt", db)

    # ── Async mode: upload audio to S3, queue the job ──
    if settings.use_async_queue:
        from app.services.sqs_client import send_job

        # Upload to S3 FIRST (in a thread — boto3 is sync and would block the
        # event loop), under a uuid key so no DB row is needed yet. If the
        # upload fails there's nothing to clean up. The worker fetches the
        # audio from this URL, so the bytes are deliberately NOT stored in the
        # DB row: Postgres is cross-region, and shipping the whole recording
        # over that link was the single largest cost of the submit path.
        if not filename or "." not in filename:
            filename = "upload.wav"
        s3_key = f"stt/{uuid.uuid4().hex}_{filename}"
        try:
            audio_url = await asyncio.to_thread(save_audio, s3_key, data)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to store audio: {str(exc)}"
            )

        # Compute queue position (number of currently queued jobs + 1)
        queue_pos = db.query(SpeechToText).filter(SpeechToText.status == "queued").count() + 1

        job = SpeechToText(
            audio_url=audio_url,
            input_format=content_type,
            user_id=user.user_id,
            status="queued",
            language_hint=language,
            webhook_url=webhook_url,
            queue_position=queue_pos,
        )
        db.add(job)
        # flush assigns the PK without ending the transaction; capture it now
        # so no post-commit refresh round-trip is needed.
        db.flush()
        job_id = job.request_id
        db.commit()

        try:
            await asyncio.to_thread(
                send_job,
                queue_url=settings.aws_sqs_stt_queue_url,
                job_id=job_id,
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
        except Exception as exc:
            # Never leave a phantom "queued" row the worker will never pick up.
            db.rollback()
            job.status = "failed"
            job.error_message = str(exc)
            job.completed_at = datetime.now(timezone.utc)
            increment_failure(user.user_id, db)
            db.commit()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to enqueue job: {str(exc)}"
            )
        return {
            "job_id": job_id,
            "status": "queued",
            "queue_position": queue_pos,
            "message": "Job submitted. Poll GET /jobs/{job_id} for status.",
        }
    # ── Sync mode: process immediately (original behavior) ──
    # audio_bytes is intentionally not stored: nothing reads it back, and the
    # blob INSERT over the cross-region DB link dominated submit latency.
    job = SpeechToText(
        audio_url="",
        input_format=content_type,
        user_id=user.user_id,
        status="processing",
        language_hint=language,
        webhook_url=webhook_url,
    )
    db.add(job)
    db.flush()
    request_id = job.request_id
    db.commit()

    start = time.perf_counter()
    try:
        audio_url = await asyncio.to_thread(save_audio, f"stt/{request_id}_{filename}", data)
        job.audio_url = audio_url

        result = await transcribe(data, filename=filename, content_type=content_type, language=language)
        job.transcript = result.get("text")
        job.detected_language = result.get("language")
        job.segments = result.get("words")
        job.processing_time = round(time.perf_counter() - start, 3)
        job.completed_at = datetime.now(timezone.utc)
        job.status = "completed"
        increment_success(user.user_id, db)
        # Read response fields BEFORE commit — afterwards they're expired and
        # each access would trigger a refresh round-trip to the remote DB.
        response = {
            "request_id": request_id,
            "detail": job.transcript,
            # Client-facing URL is presigned; the DB/SQS copies stay relative so
            # the worker's download_audio() can still resolve them as S3 keys.
            "audio_url": presign_audio_url(audio_url),
            "processing_time": job.processing_time,
            "current_time": job.created_at,
            "detected_language": job.detected_language,
            "segments": job.segments,
        }
        db.commit()
        return response
    except Exception as exc:
        # Mark the job as failed so the DB never shows status=completed with NULL results.
        db.rollback()
        job.status = "failed"
        job.error_message = str(exc)
        job.processing_time = round(time.perf_counter() - start, 3)
        job.completed_at = datetime.now(timezone.utc)
        increment_failure(user.user_id, db)
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Engine failure: {str(exc)}"
        )

