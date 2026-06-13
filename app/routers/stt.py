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

    job = SpeechToText(audio="", user_id=user.user_id)
    db.add(job)
    db.commit()
    db.refresh(job)

    # Save incoming audio file
    filename = file.filename or "upload.wav"
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
