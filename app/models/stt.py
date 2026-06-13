"""SpeechToText ORM model — `speech_to_text` table."""

from sqlalchemy import Column, Integer, String, Text, DateTime, Float, ForeignKey
from sqlalchemy.sql import func

from app.database import Base


class SpeechToText(Base):
    __tablename__ = "speech_to_text"

    request_id = Column(Integer, primary_key=True, index=True)
    audio = Column(String(512), nullable=False)         # input audio URL/path
    detail = Column(Text, nullable=True)                # output transcript
    user_id = Column(
        Integer,
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
    )
    current_time = Column(DateTime, server_default=func.now())
    updating_time = Column(DateTime, nullable=True)
    processing_time = Column(Float, nullable=True)
