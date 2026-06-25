"""Error Log ORM model — error_logs table."""

from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey
from sqlalchemy.sql import func
from app.database import Base


class ErrorLog(Base):
    __tablename__ = "error_logs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(
        Integer,
        ForeignKey("users.user_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    endpoint = Column(String(255), nullable=False)   # e.g. POST /signup
    method = Column(String(10), nullable=False)       # GET, POST etc
    error_type = Column(String(100), nullable=False)  # e.g. HTTPException, ValueError
    status_code = Column(Integer, nullable=True)      # 400, 401, 500 etc
    error_message = Column(Text, nullable=False)      # human readable message
    created_at = Column(DateTime(timezone=True), server_default=func.now())