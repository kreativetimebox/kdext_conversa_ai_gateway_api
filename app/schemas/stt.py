"""STT response schema."""

from datetime import datetime

from pydantic import BaseModel


class STTResponse(BaseModel):
    """POST /speech-to-text response body."""

    request_id: int
    detail: str
    audio_url: str
    processing_time: float
    current_time: datetime
