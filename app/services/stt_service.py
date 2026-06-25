import asyncio
import logging
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


def _extract_transcript(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("detail", "text", "transcript", "transcription"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
    raise ValueError("STT engine response did not include transcript text")


async def transcribe(
    audio_bytes: bytes,
    filename: str = "audio.wav",
    content_type: str = "audio/wav",
    language: str | None = None,
) -> dict[str, Any]:
    """Transcribe audio via the configured STT engine, or local stub in dev."""
    if settings.stt_engine_url:
        data = {}
        if language:
            data["language"] = language
        logger.info("Proxying STT request to engine: language=%s", language or "auto")
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{settings.stt_engine_url.rstrip('/')}{settings.stt_engine_path}",
                files={"file": (filename, audio_bytes, content_type)},
                data=data,
                timeout=settings.engine_timeout_seconds,
            )
            resp.raise_for_status()
            content_type_header = resp.headers.get("content-type", "")
            if "application/json" in content_type_header:
                return resp.json()
            return {"text": resp.text.strip(), "language": None, "words": []}

    logger.warning("STT_ENGINE_URL is not configured; using development stub transcriber")
    await asyncio.sleep(0.5)
    return {"text": "Hello, welcome to the voice gateway.", "language": "en", "words": []}

