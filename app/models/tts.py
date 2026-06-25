"""TextToSpeech ORM model — `text_to_speech` table."""

from sqlalchemy import Column, Integer, String, Text, DateTime, Float, ForeignKey, LargeBinary
from sqlalchemy.sql import func

from app.database import Base


class TextToSpeech(Base):
    __tablename__ = "text_to_speech"

    request_id = Column(Integer, primary_key=True, index=True)
    audio_url = Column(String(512), nullable=True)      # generated audio URL/path
    audio_bytes = Column(LargeBinary, nullable=True)    # raw generated audio bytes
    input_text = Column(Text, nullable=False)           # input text
    user_id = Column(
        Integer,
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
    )
    language = Column(String(10), nullable=True)
    model_used = Column(String(50), nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    completed_at = Column(DateTime, nullable=True)
    processing_time = Column(Float, nullable=True)

    # Async job queue fields
    status = Column(String(20), default="completed", nullable=False, index=True)
    error_message = Column(Text, nullable=True)
    voice = Column(String(256), nullable=True)
    format = Column(String(16), default="wav", nullable=True)
    queue_position = Column(Integer, nullable=True)

    # Webhook callback fields
    webhook_url = Column(String(2048), nullable=True)
    webhook_sent_at = Column(DateTime, nullable=True)


