import asyncio
import time
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from app.database import get_db
from app.core.dependencies import verify_api_key_cached
from app.models.user import User
from app.models.tts import TextToSpeech
from app.schemas.tts import TTSRequest
from app.services.tts_service import synthesize, SPEAKERS, get_voice_info
from app.storage.audio_store import save_audio
from app.services.usage import increment_success, increment_failure
from app.services.rate_limiter import check_rate_limit
from app.config import get_settings

router = APIRouter(tags=["tts"])
settings = get_settings()


@router.get("/voices")
async def list_voices():
    """Return all available TTS speaker voices.

    Use the speaker name (e.g. 'rohit', 'divya') as the `voice` field
    in your POST /text-to-speech request.  Optionally prefix with a language
    code: 'hi-rohit' routes to Hindi Indic-Parler with Rohit's voice.
    """
    return {
        "voices": [
            {
                "id": name,
                "name": name.capitalize().replace("_", " "),
                "gender": info["gender"],
                "style": info["style"],
                "language": info["language"],
                "example_voice_id": name,
                "model": info.get("model", "indic_parler"),
            }
            for name, info in SPEAKERS.items()
        ],
        "usage": "Pass the voice 'id' as the `voice` field in POST /text-to-speech",
        "example": {"text": "नमस्ते, मैं रोहित हूँ।", "voice": "rohit"},
    }


@router.post("/text-to-speech")
async def text_to_speech(body: TTSRequest,
                         user: User = Depends(verify_api_key_cached),
                         db: Session = Depends(get_db)):
    # Rate-limit before creating any job row — applies to sync AND async mode.
    check_rate_limit(user.user_id, "tts", db)

    # ── Async mode: queue the job and return immediately ──
    if settings.use_async_queue:
        from app.services.sqs_client import send_job

        # Compute queue position (number of currently queued jobs + 1)
        queue_pos = db.query(TextToSpeech).filter(TextToSpeech.status == "queued").count() + 1
        lang, model = get_voice_info(body.voice)

        job = TextToSpeech(
            input_text=body.text,
            user_id=user.user_id,
            status="queued",
            voice=body.voice,
            format=body.format,
            language=lang,
            model_used=model,
            webhook_url=body.webhook_url,
            queue_position=queue_pos,
        )
        db.add(job)
        # flush assigns the PK without ending the transaction; capture it now
        # so no post-commit refresh round-trip is needed.
        db.flush()
        job_id = job.request_id
        db.commit()

        try:
            # boto3 is sync — run in a thread so the event loop stays free.
            await asyncio.to_thread(
                send_job,
                queue_url=settings.aws_sqs_tts_queue_url,
                job_id=job_id,
                job_type="tts",
                payload={
                    "text": body.text,
                    "voice": body.voice,
                    "format": body.format,
                    "user_id": user.user_id,
                    "webhook_url": body.webhook_url,
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
    lang, model = get_voice_info(body.voice)
    job = TextToSpeech(
        input_text=body.text,
        user_id=user.user_id,
        status="processing",
        voice=body.voice,
        format=body.format,
        language=lang,
        model_used=model,
        webhook_url=body.webhook_url,
    )
    db.add(job)
    db.flush()
    request_id = job.request_id
    db.commit()

    start = time.perf_counter()
    try:
        audio_bytes = await synthesize(body.text, body.voice, body.format)
        # boto3 is sync — run in a thread so the event loop stays free. The
        # generated audio is served from S3 via audio_url; storing the bytes in
        # the cross-region DB as well was pure added latency, so it's skipped.
        audio_url = await asyncio.to_thread(
            save_audio, f"tts/{request_id}.{body.format}", audio_bytes
        )

        job.audio_url = audio_url
        job.processing_time = round(time.perf_counter() - start, 3)
        job.completed_at = datetime.now(timezone.utc)
        job.status = "completed"
        increment_success(user.user_id, db)
        # Read response fields BEFORE commit — afterwards they're expired and
        # each access would trigger a refresh round-trip to the remote DB.
        response = {
            "request_id": request_id,
            "audio_url": audio_url,
            "detail": job.input_text,
            "processing_time": job.processing_time,
            "current_time": job.created_at,
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

