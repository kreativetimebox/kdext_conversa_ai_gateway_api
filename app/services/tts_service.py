"""TTS gateway service — voice resolution and engine proxy."""

import httpx
import logging
from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# ---------------------------------------------------------------------------
# Language routing map
# ---------------------------------------------------------------------------
SUPPORTED_LANGS = {
    "as", "bn", "brx", "doi", "gu", "hi", "kn", "kok", "ks", "mai", "ml", "mni",
    "mr", "ne", "or", "pa", "sa", "sat", "sd", "ta", "te", "ur",   # Indic → Parler
    "ar", "de", "en", "es", "fr", "it", "ja", "ko", "nl", "pl", "pt", "ru", "tr", "zh",  # → Bark
}

# ---------------------------------------------------------------------------
# Named speaker registry
#
# These are real speaker names from ai4bharat/indic-parler-tts.  Using named
# speakers produces dramatically more consistent voice across chunks compared
# to open-ended descriptions, because the model maps them to a fixed pre-trained
# speaker embedding rather than sampling from a generic description space.
#
# Each entry has:
#   description : str   — the prompt sent to Indic-Parler-TTS
#   gender      : str   — "female" | "male"
#   language    : str   — primary language ("multilingual" if works across langs)
#   style       : str   — human-readable style label shown in /voices response
# ---------------------------------------------------------------------------

SPEAKERS: dict[str, dict] = {
    # ── Female speakers ──────────────────────────────────────────────────────
    "divya": {
        "description": (
            "Divya's voice is monotone yet slightly fast in delivery, "
            "with a very close recording that almost has no background noise."
        ),
        "gender": "female",
        "language": "multilingual",
        "style": "Monotone, fast",
    },
    "sita": {
        "description": (
            "Sita's voice is calm and slow with clear articulation, "
            "with a very close recording that almost has no background noise."
        ),
        "gender": "female",
        "language": "multilingual",
        "style": "Calm, slow",
    },
    "meera": {
        "description": (
            "Meera's voice is expressive and warm with a moderate pace, "
            "with a very close recording that almost has no background noise."
        ),
        "gender": "female",
        "language": "multilingual",
        "style": "Expressive, warm",
    },
    "priya": {
        "description": (
            "Priya's voice is clear and professional with a moderate speed, "
            "with a very close recording that almost has no background noise."
        ),
        "gender": "female",
        "language": "multilingual",
        "style": "Clear, professional",
    },
    # ── Male speakers ────────────────────────────────────────────────────────
    "rohit": {
        "description": (
            "Rohit's voice is calm and neutral with a moderate pace, "
            "with a very close recording that almost has no background noise."
        ),
        "gender": "male",
        "language": "multilingual",
        "style": "Calm, neutral",
    },
    "arjun": {
        "description": (
            "Arjun's voice is deep and slow with clear articulation, "
            "with a very close recording that almost has no background noise."
        ),
        "gender": "male",
        "language": "multilingual",
        "style": "Deep, slow",
    },
    "vikram": {
        "description": (
            "Vikram's voice is confident and expressive with a moderate pace, "
            "with a very close recording that almost has no background noise."
        ),
        "gender": "male",
        "language": "multilingual",
        "style": "Confident, expressive",
    },
    "amir": {
        "description": (
            "Amir's voice is clear and slightly fast with a neutral tone, "
            "with a very close recording that almost has no background noise."
        ),
        "gender": "male",
        "language": "multilingual",
        "style": "Clear, slightly fast",
    },
}

# Default speaker when none is specified or the name is not found
DEFAULT_SPEAKER = "divya"


def get_speaker_description(speaker_name: str) -> str:
    """Return the Parler-TTS description for a named speaker.

    Accepts the speaker name case-insensitively.  Falls back to the default
    speaker if the name is not in the registry.
    """
    key = speaker_name.strip().lower()
    if key in SPEAKERS:
        return SPEAKERS[key]["description"]

    # Legacy compat: if it looks like an old voice ID (e.g. "hi-female-1")
    # try to pick a reasonable default by gender
    if "female" in key:
        return SPEAKERS["divya"]["description"]
    if "male" in key:
        return SPEAKERS["rohit"]["description"]

    # If it's already a long Parler description, pass it through
    if len(speaker_name) > 40:
        return speaker_name

    logger.warning(f"Unknown speaker '{speaker_name}', falling back to '{DEFAULT_SPEAKER}'")
    return SPEAKERS[DEFAULT_SPEAKER]["description"]


async def synthesize(text: str, voice: str, format: str) -> bytes:
    """Proxy the TTS synthesis request to the underlying engine."""

    # Extract language code from voice string for routing
    # Supports: "rohit" (name only), "hi-rohit", "hi-female-1" (legacy)
    lang = "hi"  # default to Hindi for Indic speaker names
    voice_lower = voice.strip().lower().replace("_", "-") if voice else ""

    parts = voice_lower.split("-")
    if parts and parts[0] in SUPPORTED_LANGS:
        lang = parts[0]
        # remaining part is the speaker name
        speaker_part = "-".join(parts[1:]) if len(parts) > 1 else DEFAULT_SPEAKER
    else:
        # No language prefix — treat the whole string as a speaker name
        speaker_part = voice_lower or DEFAULT_SPEAKER

    parler_voice = get_speaker_description(speaker_part)

    logger.info(
        f"TTS → engine | lang={lang} speaker={speaker_part!r} "
        f"description={parler_voice[:60]!r}..."
    )

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{settings.tts_engine_url.rstrip('/')}{settings.tts_engine_path}",
            json={
                "text": text,
                "language": lang,
                "voice": parler_voice,
            },
            timeout=settings.engine_timeout_seconds,
        )
        resp.raise_for_status()
        return resp.content


def get_voice_info(voice: str) -> tuple[str, str]:
    """Return a tuple of (language_code, model_used) for a given voice string."""
    lang = "hi"  # default to Hindi for Indic speaker names
    voice_lower = voice.strip().lower().replace("_", "-") if voice else ""

    parts = voice_lower.split("-")
    if parts and parts[0] in SUPPORTED_LANGS:
        lang = parts[0]

    # Indic-Parler-TTS languages
    indic_langs = {
        "as", "bn", "brx", "doi", "gu", "hi", "kn", "kok", "ks", "mai", "ml", "mni",
        "mr", "ne", "or", "pa", "sa", "sat", "sd", "ta", "te", "ur"
    }
    model = "indic_parler" if lang in indic_langs else "bark"
    return lang, model

