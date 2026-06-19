"""Demo endpoints — no authentication required.

These exist purely for interactive testing of the TTS and STT engines
through the browser-based demo page served at GET /demo.
"""

import logging
from pathlib import Path

import httpx
from fastapi import APIRouter, File, Form, UploadFile, HTTPException, status
from fastapi.responses import HTMLResponse, Response

from app.config import get_settings
from app.services.tts_service import get_speaker_description

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/demo", tags=["demo"])
settings = get_settings()

DEMO_HTML = Path(__file__).resolve().parent.parent / "static" / "demo.html"


@router.get("/voices")
async def list_voices():
    """Return all available TTS speaker voices — no auth required.

    Used by the demo page to populate the speaker picker.
    """
    from app.services.tts_service import SPEAKERS
    return {
        "voices": [
            {
                "id": name,
                "name": name.capitalize(),
                "gender": info["gender"],
                "style": info["style"],
                "language": info["language"],
            }
            for name, info in SPEAKERS.items()
        ],
        "usage": "Pass the voice 'id' as the `voice` field in POST /text-to-speech",
        "example": {"text": "\u0928\u092e\u0938\u094d\u0924\u0947", "voice": "rohit"},
    }


@router.get("", response_class=HTMLResponse, include_in_schema=False)
async def demo_page():
    """Serve the interactive demo page."""
    return HTMLResponse(DEMO_HTML.read_text(encoding="utf-8"))


@router.post("/tts")
async def demo_tts(text: str = Form(...),
                   language: str = Form(default="en"),
                   voice: str = Form(default="en-US-female-1")):
    """Proxy TTS request to the engine and return raw audio bytes."""
    # Resolve speaker name (e.g. 'rohit') to a Parler-TTS natural-language
    # description before forwarding — the engine needs the full description,
    # not an opaque ID.
    parler_voice = get_speaker_description(voice)

    url = f"{settings.tts_engine_url.rstrip('/')}{settings.tts_engine_path}"
    logger.info("Demo TTS → %s  lang=%s speaker=%s", url, language, voice)
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url,
                json={"text": text, "language": language, "voice": parler_voice},
                timeout=settings.engine_timeout_seconds,
            )
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "audio/wav")
            return Response(content=resp.content, media_type=content_type)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=exc.response.status_code,
            detail=f"TTS engine error: {exc.response.text[:500]}",
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"TTS engine unreachable: {exc}",
        )


@router.post("/stt")
async def demo_stt(file: UploadFile = File(...),
                   language: str = Form(default=None)):
    """Proxy STT request to the engine and return the transcript."""
    if not settings.stt_engine_url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="STT engine URL is not configured",
        )
    url = f"{settings.stt_engine_url.rstrip('/')}{settings.stt_engine_path}"
    data = await file.read()
    filename = file.filename or "audio.wav"
    content_type = file.content_type or "audio/wav"
    form_data = {}
    if language:
        form_data["language"] = language

    logger.info("Demo STT → %s  filename=%s lang=%s", url, filename, language or "auto")
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url,
                files={"file": (filename, data, content_type)},
                data=form_data,
                timeout=settings.engine_timeout_seconds,
            )
            resp.raise_for_status()
            ct = resp.headers.get("content-type", "")
            if "application/json" in ct:
                return resp.json()
            return {"text": resp.text.strip()}
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=exc.response.status_code,
            detail=f"STT engine error: {exc.response.text[:500]}",
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"STT engine unreachable: {exc}",
        )


@router.get("/health")
async def demo_health():
    """Quick connectivity check against both engines."""
    results = {}
    for label, url in [("tts", settings.tts_engine_url), ("stt", settings.stt_engine_url)]:
        if not url:
            results[label] = {"status": "not_configured"}
            continue
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(url, timeout=5)
                results[label] = {"status": "ok", "http": r.status_code}
        except Exception as exc:
            results[label] = {"status": "unreachable", "error": str(exc)}
    return results
