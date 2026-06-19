"""TTS request/response schemas."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.config import get_settings


class TTSRequest(BaseModel):
    """POST /text-to-speech request body."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(..., min_length=1, description="Text to synthesize")
    voice: str = Field(
        default="divya",
        max_length=256,
        description=(
            "Speaker name (e.g. 'rohit', 'divya', 'sita', 'arjun'). "
            "See GET /voices for the full list. "
            "Optionally prefix with a language code: 'hi-rohit'."
        ),
    )
    format: str = Field(default="wav", min_length=2, max_length=16, description="Output audio format")

    @field_validator("text")
    @classmethod
    def validate_text_length(cls, value: str) -> str:
        max_chars = get_settings().max_tts_text_chars
        if len(value.strip()) > max_chars:
            raise ValueError(f"text must be at most {max_chars} characters")
        return value

    @field_validator("format")
    @classmethod
    def validate_format(cls, value: str) -> str:
        requested = value.lower()
        allowed = {item.lower() for item in get_settings().tts_allowed_formats}
        if requested not in allowed:
            allowed_list = ", ".join(sorted(allowed))
            raise ValueError(f"Unsupported audio format. Allowed formats: {allowed_list}")
        return requested



class TTSResponse(BaseModel):
    """POST /text-to-speech response body."""

    request_id: int
    audio_url: str
    detail: str
    processing_time: float
    current_time: datetime
