import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from app.database import Base, engine
from app.routers import auth, profile, tts, stt
from app.config import get_settings
from app.core.logging import configure_logging

settings = get_settings()
configure_logging(settings.log_level)

if settings.create_db_tables:
    Base.metadata.create_all(bind=engine)

# Ensure audio storage folder exists (used as local fallback when S3 is off)
if not settings.use_s3_storage:
    os.makedirs(settings.audio_storage_dir, exist_ok=True)

app = FastAPI(title="Voice Gateway API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount the local audio directory only when NOT using S3.
# In S3 mode, audio_url values are full HTTPS URLs — no local serving needed.
if not settings.use_s3_storage:
    app.mount("/audio", StaticFiles(directory=settings.audio_storage_dir), name="audio")

app.include_router(auth.router)
app.include_router(profile.router)
app.include_router(tts.router)
app.include_router(stt.router)

# Demo router — unauthenticated proxy endpoints for testing.
# Set DISABLE_DEMO=true in production to remove /demo/* routes entirely.
_disable_demo = os.environ.get("DISABLE_DEMO", "false").lower() == "true"
if not _disable_demo:
    from app.routers import demo  # noqa: E402
    app.include_router(demo.router)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/ready")
def ready():
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    return {"status": "ready"}
