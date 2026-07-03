"""SQLAlchemy engine, session factory, and declarative base."""

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config import get_settings

settings = get_settings()

# SQLite requires connect_args for thread safety; PostgreSQL does not.
# client_encoding=utf8 ensures multi-byte Unicode text (Indic scripts, emoji, etc.)
# is stored and retrieved correctly regardless of the server OS locale.
connect_args = {}
engine_kwargs = {"pool_pre_ping": True}
if settings.database_url.startswith("sqlite"):
    connect_args = {"check_same_thread": False}
else:
    connect_args = {"client_encoding": "utf8"}
    # Connection pool sized for concurrent requests across workers. pool_recycle
    # avoids reusing stale RDS connections that the DB has already dropped.
    engine_kwargs.update(
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_recycle=settings.db_pool_recycle,
    )

engine = create_engine(
    settings.database_url,
    connect_args=connect_args,
    **engine_kwargs,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    """Yield a scoped database session and ensure cleanup."""

    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
