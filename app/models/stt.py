"""SpeechToText ORM model — `speech_to_text` table."""

from sqlalchemy import Column, Integer, String, Text, DateTime, Float, ForeignKey, LargeBinary, JSON
from sqlalchemy.sql import func

from app.database import Base


class SpeechToText(Base):
    __tablename__ = "speech_to_text"

    request_id = Column(Integer, primary_key=True, index=True)
    audio_url = Column(String(512), nullable=False)     # input audio URL/path
    audio_bytes = Column(LargeBinary, nullable=True)    # raw input audio bytes
    input_format = Column(String(20), nullable=True)    # format/MIME type
    transcript = Column(Text, nullable=True)            # output transcript
    user_id = Column(
        Integer,
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    completed_at = Column(DateTime, nullable=True)
    processing_time = Column(Float, nullable=True)

    # Async job queue fields
    status = Column(String(20), default="completed", nullable=False, index=True)
    error_message = Column(Text, nullable=True)
    language_hint = Column(String(10), nullable=True)
    detected_language = Column(String(10), nullable=True)
    segments = Column(JSON, nullable=True)
    queue_position = Column(Integer, nullable=True)

    # Webhook callback fields
    webhook_url = Column(String(2048), nullable=True)
    webhook_sent_at = Column(DateTime, nullable=True)


