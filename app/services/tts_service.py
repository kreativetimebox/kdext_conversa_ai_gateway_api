import httpx
import logging
from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# List of supported languages in the TTS engine
SUPPORTED_LANGS = {
    "as", "bn", "brx", "doi", "gu", "hi", "kn", "kok", "ks", "mai", "ml", "mni",
    "mr", "ne", "or", "pa", "sa", "sat", "sd", "ta", "te", "ur", "ar", "de",
    "en", "es", "fr", "it", "ja", "ko", "nl", "pl", "pt", "ru", "tr", "zh"
}

async def synthesize(text: str, voice: str, format: str) -> bytes:
    """Proxies the TTS generation request to the underlying TTS engine."""
    # Try to extract language from voice name, e.g. "en-US-female-1" -> "en"
    lang = "en"
    if voice:
        parts = voice.replace("_", "-").split("-")
        if parts:
            possible_lang = parts[0].lower()
            if possible_lang in SUPPORTED_LANGS:
                lang = possible_lang

    logger.info(f"Proxying TTS request to engine: language={lang}, voice={voice}")
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{settings.tts_engine_url.rstrip('/')}{settings.tts_engine_path}",
            json={
                "text": text,
                "language": lang,
                "voice": voice,
            },
            timeout=settings.engine_timeout_seconds,
        )
        resp.raise_for_status()
        return resp.content
