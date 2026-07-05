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
    "de", "en", "es", "fr", "it", "ja", "ko", "pt", "ru", "zh",  # → Qwen3-TTS-CustomVoice
}

# ---------------------------------------------------------------------------
# Named speaker registry
#
# Indic speakers are real speaker names from ai4bharat/indic-parler-tts; using
# named speakers produces dramatically more consistent voice across chunks
# compared to open-ended descriptions, because the model maps them to a fixed
# pre-trained speaker embedding rather than sampling from a generic
# description space. Non-Indic speakers are the 9 named presets shipped with
# Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice.
#
# Each entry has:
#   description : str   — the prompt (Indic-Parler) or speaker id (Qwen3-TTS)
#                          sent to the TTS engine
#   gender      : str   — "female" | "male"
#   language    : str   — primary language ("multilingual" if works across langs)
#   style       : str   — human-readable style label shown in /voices response
# ---------------------------------------------------------------------------

SPEAKERS: dict[str, dict] = {
    # ── Indic-Parler speakers ────────────────────────────────────────────────
    "divya": {
        "description": (
            "Divya's voice is monotone yet slightly fast in delivery, "
            "with a very close recording that almost has no background noise."
        ),
        "gender": "female",
        "language": "multilingual",
        "style": "Monotone, fast",
        "model": "indic_parler",
    },
    "sita": {
        "description": (
            "Sita's voice is calm and slow with clear articulation, "
            "with a very close recording that almost has no background noise."
        ),
        "gender": "female",
        "language": "multilingual",
        "style": "Calm, slow",
        "model": "indic_parler",
    },
    "meera": {
        "description": (
            "Meera's voice is expressive and warm with a moderate pace, "
            "with a very close recording that almost has no background noise."
        ),
        "gender": "female",
        "language": "multilingual",
        "style": "Expressive, warm",
        "model": "indic_parler",
    },
    "priya": {
        "description": (
            "Priya's voice is clear and professional with a moderate speed, "
            "with a very close recording that almost has no background noise."
        ),
        "gender": "female",
        "language": "multilingual",
        "style": "Clear, professional",
        "model": "indic_parler",
    },
    "rohit": {
        "description": (
            "Rohit's voice is calm and neutral with a moderate pace, "
            "with a very close recording that almost has no background noise."
        ),
        "gender": "male",
        "language": "multilingual",
        "style": "Calm, neutral",
        "model": "indic_parler",
    },
    "arjun": {
        "description": (
            "Arjun's voice is deep and slow with clear articulation, "
            "with a very close recording that almost has no background noise."
        ),
        "gender": "male",
        "language": "multilingual",
        "style": "Deep, slow",
        "model": "indic_parler",
    },
    "vikram": {
        "description": (
            "Vikram's voice is confident and expressive with a moderate pace, "
            "with a very close recording that almost has no background noise."
        ),
        "gender": "male",
        "language": "multilingual",
        "style": "Confident, expressive",
        "model": "indic_parler",
    },
    "amir": {
        "description": (
            "Amir's voice is clear and slightly fast with a neutral tone, "
            "with a very close recording that almost has no background noise."
        ),
        "gender": "male",
        "language": "multilingual",
        "style": "Clear, slightly fast",
        "model": "indic_parler",
    },
    # ── Qwen3-TTS-CustomVoice speakers ──────────────────────────────────────
    # "description" here is the literal speaker name qwen-tts expects — unlike
    # Indic-Parler, Qwen3-TTS-CustomVoice takes a named speaker id directly,
    # not a free-text description. Any speaker can render any of the 10
    # supported global languages (cross-lingual), hence "multilingual" below.
    "vivian": {
        "description": "vivian",
        "gender": "female",
        "language": "multilingual",
        "style": "Bright, slightly edgy young female",
        "model": "qwen_custom_voice",
    },
    "serena": {
        "description": "serena",
        "gender": "female",
        "language": "multilingual",
        "style": "Warm, gentle young female",
        "model": "qwen_custom_voice",
    },
    "ono_anna": {
        "description": "ono_anna",
        "gender": "female",
        "language": "multilingual",
        "style": "Playful, light and nimble timbre",
        "model": "qwen_custom_voice",
    },
    "sohee": {
        "description": "sohee",
        "gender": "female",
        "language": "multilingual",
        "style": "Warm, rich emotion",
        "model": "qwen_custom_voice",
    },
    "uncle_fu": {
        "description": "uncle_fu",
        "gender": "male",
        "language": "multilingual",
        "style": "Seasoned, low mellow timbre",
        "model": "qwen_custom_voice",
    },
    "dylan": {
        "description": "dylan",
        "gender": "male",
        "language": "multilingual",
        "style": "Youthful, clear natural timbre",
        "model": "qwen_custom_voice",
    },
    "eric": {
        "description": "eric",
        "gender": "male",
        "language": "multilingual",
        "style": "Lively, slightly husky brightness",
        "model": "qwen_custom_voice",
    },
    "ryan": {
        "description": "ryan",
        "gender": "male",
        "language": "multilingual",
        "style": "Dynamic, strong rhythmic drive",
        "model": "qwen_custom_voice",
    },
    "aiden": {
        "description": "aiden",
        "gender": "male",
        "language": "multilingual",
        "style": "Sunny, clear midrange",
        "model": "qwen_custom_voice",
    },
}

# Default Qwen3-TTS-CustomVoice speaker when the requested one isn't recognized.
DEFAULT_QWEN_SPEAKER = "ryan"

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

    # Indic-Parler-TTS languages
    indic_langs = {
        "as", "bn", "brx", "doi", "gu", "hi", "kn", "kok", "ks", "mai", "ml", "mni",
        "mr", "ne", "or", "pa", "sa", "sat", "sd", "ta", "te", "ur"
    }
    model = "indic_parler" if lang in indic_langs else "qwen_custom_voice"

    if model == "indic_parler":
        parler_voice = get_speaker_description(speaker_part)
    else:
        # For Qwen3-TTS-CustomVoice, resolve the name to one of its 9 named
        # speakers — e.g. "ono_anna" or "ono-anna" -> "ono_anna".
        normalized_part = speaker_part.replace("-", "_")
        if normalized_part in SPEAKERS and SPEAKERS[normalized_part].get("model") == "qwen_custom_voice":
            parler_voice = SPEAKERS[normalized_part]["description"]
        else:
            # Map default or unrecognized voices to a Qwen speaker by gender
            key = normalized_part
            if key in SPEAKERS:
                gender = SPEAKERS[key].get("gender", "male")
            else:
                gender = "female" if "female" in key else "male"

            parler_voice = "vivian" if gender == "female" else DEFAULT_QWEN_SPEAKER

    logger.info(
        f"TTS → engine | lang={lang} speaker={speaker_part!r} "
        f"resolved_voice={parler_voice!r}..."
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
    model = "indic_parler" if lang in indic_langs else "qwen_custom_voice"
    return lang, model

