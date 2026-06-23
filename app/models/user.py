"""User ORM model — `users` table."""

from sqlalchemy import Column, Integer, String, DateTime,Boolean

from app.database import Base


class User(Base):
    __tablename__ = "users"

    user_id = Column(Integer, primary_key=True, index=True)
    password = Column(String(255), nullable=False)
    email = Column(String(255), unique=True, nullable=False, index=True)
    api_key = Column(String(64), unique=True, nullable=False, index=True)
    login_time = Column(DateTime, nullable=True)
    signout_time = Column(DateTime, nullable=True)
    total_processing = Column(Integer, default=0, nullable=False)
    total_failed = Column(Integer, default=0, nullable=False)
    is_verified = Column(Boolean, default=False, nullable=False)
