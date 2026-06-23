import time
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from app.database import get_db
from app.core.dependencies import verify_api_key
from app.models.user import User
from app.models.tts import TextToSpeech
from app.schemas.tts import TTSRequest
from app.services.tts_service import synthesize, SPEAKERS
from app.storage.audio_store import save_audio
from app.services.usage import increment_success, increment_failure
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
                "name": name.capitalize(),
                "gender": info["gender"],
                "style": info["style"],
                "language": info["language"],
                "example_voice_id": name,
            }
            for name, info in SPEAKERS.items()
        ],
        "usage": "Pass the voice 'id' as the `voice` field in POST /text-to-speech",
        "example": {"text": "नमस्ते, मैं रोहित हूँ।", "voice": "rohit"},
    }


@router.post("/text-to-speech")
async def text_to_speech(body: TTSRequest,
                         user: User = Depends(verify_api_key),
                         db: Session = Depends(get_db)):
    # ── Async mode: queue the job and return immediately ──
    if settings.use_async_queue:
        from app.services.sqs_client import send_job

        # Compute queue position (number of currently queued jobs + 1)
        queue_pos = db.query(TextToSpeech).filter(TextToSpeech.status == "queued").count() + 1

        job = TextToSpeech(
            detail=body.text,
            user_id=user.user_id,
            status="queued",
            voice=body.voice,
            format=body.format,
            webhook_url=body.webhook_url,
            queue_position=queue_pos,
        )
        db.add(job)
        db.commit()
        db.refresh(job)

        send_job(
            queue_url=settings.aws_sqs_tts_queue_url,
            job_id=job.request_id,
            job_type="tts",
            payload={
                "text": body.text,
                "voice": body.voice,
                "format": body.format,
                "user_id": user.user_id,
                "webhook_url": body.webhook_url,
            },
        )
        return {
            "job_id": job.request_id,
            "status": "queued",
            "queue_position": queue_pos,
            "message": "Job submitted. Poll GET /jobs/{job_id} for status.",
        }

    # ── Sync mode: process immediately (original behavior) ──
    job = TextToSpeech(detail=body.text, user_id=user.user_id)
    db.add(job)
    db.commit()
    db.refresh(job)

    start = time.perf_counter()
    try:
        audio_bytes = await synthesize(body.text, body.voice, body.format)
        audio_url = save_audio(f"tts/{job.request_id}.{body.format}", audio_bytes)

        job.audio = audio_url
        job.processing_time = round(time.perf_counter() - start, 3)
        job.updating_time = datetime.now(timezone.utc)
        increment_success(user.user_id, db)
        db.commit()
        return {
            "request_id": job.request_id,
            "audio_url": audio_url,
            "detail": job.detail,
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
