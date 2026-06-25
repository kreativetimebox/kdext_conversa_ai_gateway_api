"""Rate limit tracking per user."""

from sqlalchemy import Column, Integer, String, DateTime, ForeignKey
from sqlalchemy.sql import func
from app.database import Base


class RateLimit(Base):
    __tablename__ = "rate_limits"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(
        Integer,
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    endpoint = Column(String(50), nullable=False)  # "tts" or "stt"
    window_minute = Column(String(20), nullable=False)  # "2026-06-24-14-30"
    window_day = Column(String(10), nullable=False)     # "2026-06-24"
    rpm_count = Column(Integer, default=0, nullable=False)
    rpd_count = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())