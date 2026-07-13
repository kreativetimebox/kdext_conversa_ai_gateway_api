import logging
import os

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException as FastAPIHTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.database import Base, engine, SessionLocal
from app.models import rate_limit  # noqa: F401 — registers table
from app.models import error_log  # noqa: F401 — registers table
from app.models import conversation  # noqa: F401 — registers conversations + chat_messages
from app.routers import auth, profile, tts, stt, jobs, chatbot, history, documents
from app.config import get_settings
from app.core.logging import configure_logging
from app.services.error_logger import log_error

logger = logging.getLogger(__name__)

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

# Fast file size validation via Content-Length header before downloading file data
@app.middleware("http")
async def limit_upload_size(request: Request, call_next):
    if request.url.path in ("/speech-to-text", "/demo/stt"):
        if request.method == "POST":
            content_length = request.headers.get("content-length")
            if content_length:
                try:
                    if int(content_length) > settings.max_audio_upload_bytes:
                        return JSONResponse(
                            status_code=413,
                            content={"detail": "Uploaded audio file is too large"}
                        )
                except ValueError:
                    pass
    return await call_next(request)

from fastapi import HTTPException
from fastapi.responses import FileResponse, StreamingResponse

@app.get("/audio/{path:path}")
async def get_audio_file(path: str):
    """Retrieve and stream audio file from local filesystem or private S3 bucket."""
    if not settings.use_s3_storage:
        # Serve local files
        local_path = os.path.abspath(os.path.join(settings.audio_storage_dir, path))
        storage_root = os.path.abspath(settings.audio_storage_dir)
        
        # Security check: prevent directory traversal
        if os.path.commonpath([storage_root, local_path]) != storage_root:
            raise HTTPException(status_code=403, detail="Access denied")
            
        if not os.path.exists(local_path):
            raise HTTPException(status_code=404, detail="Audio file not found")
            
        content_type = "audio/wav"
        if path.endswith(".mp3"):
            content_type = "audio/mpeg"
        return FileResponse(local_path, media_type=content_type)

    # Serve from S3 (Gateway reverse-proxies S3)
    bucket = settings.aws_s3_bucket
    region = settings.aws_s3_region
    try:
        import boto3
        # Create boto3 client
        s3 = boto3.client(
            "s3",
            region_name=region,
            aws_access_key_id=settings.aws_access_key_id,
            aws_secret_access_key=settings.aws_secret_access_key,
        )
        # Fetch object stream
        response = s3.get_object(Bucket=bucket, Key=path)
        content_type = response.get("ContentType", "audio/wav")
        
        # Stream it back
        return StreamingResponse(
            response["Body"],
            media_type=content_type,
            headers={
                "Content-Length": str(response.get("ContentLength", "")),
                "Accept-Ranges": "bytes",
            }
        )
    except Exception as exc:
        logger.error("Failed to stream S3 audio path %s from bucket %s: %s", path, bucket, exc)
        raise HTTPException(status_code=404, detail="Audio file not found or inaccessible")

app.include_router(auth.router)
app.include_router(profile.router)
app.include_router(tts.router)
app.include_router(stt.router)
app.include_router(jobs.router)

# Chat history storage (/conversations/*) — persists chats/translations in the DB.
# Registered BEFORE the proxy so its routes are matched first (it lives at root,
# not /api, so there is no collision anyway).
app.include_router(history.router)

# Document chat (/documents/*) — reference an OCR-scanned document by request
# id, then ask questions answered from its scanned content via the LLM service.
app.include_router(documents.router)

# Chatbot LLM + voice surface (/api/chat, /api/translate, /api/voice/*, /v1/*),
# reverse-proxied to the LLM service with the gateway's API-key management.
app.include_router(chatbot.router)

# Demo router — unauthenticated proxy endpoints for testing.
# Set DISABLE_DEMO=true in production to remove /demo/* routes entirely.
_disable_demo = os.environ.get("DISABLE_DEMO", "false").lower() == "true"
if not _disable_demo:
    from app.routers import demo  # noqa: E402
    app.include_router(demo.router)


# ---------------------------------------------------------------------------
# Global exception handlers — log all errors to DB for observability
# ---------------------------------------------------------------------------

@app.exception_handler(FastAPIHTTPException)
async def http_exception_handler(request: Request, exc: FastAPIHTTPException) -> JSONResponse:
    """Catch all HTTPExceptions and log to DB."""
    logger.warning(
        "http_exception endpoint=%s method=%s status=%s message=%s",
        request.url.path,
        request.method,
        exc.status_code,
        str(exc.detail),
    )

    db = SessionLocal()
    try:
        log_error(
            db=db,
            endpoint=str(request.url.path),
            method=request.method,
            error_type="HTTPException",
            error_message=str(exc.detail),
            status_code=exc.status_code,
        )
    except Exception as db_exc:
        logger.error("failed to log http error to db: %s", str(db_exc))
    finally:
        db.close()

    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": "HTTPException",
            "message": exc.detail,
            "path": str(request.url.path),
        },
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch all unhandled exceptions, log to DB, return clean error response."""
    error_type = type(exc).__name__
    error_message = str(exc)
    status_code = getattr(exc, "status_code", 500)

    logger.error(
        "unhandled_exception endpoint=%s method=%s error=%s message=%s",
        request.url.path,
        request.method,
        error_type,
        error_message,
    )

    db = SessionLocal()
    try:
        log_error(
            db=db,
            endpoint=str(request.url.path),
            method=request.method,
            error_type=error_type,
            error_message=error_message,
            status_code=status_code,
        )
    except Exception as db_exc:
        logger.error("failed to log error to db: %s", str(db_exc))
    finally:
        db.close()

    return JSONResponse(
        status_code=status_code if isinstance(status_code, int) else 500,
        content={
            "error": error_type,
            "message": error_message,
            "path": str(request.url.path),
        },
    )


@app.on_event("shutdown")
async def _shutdown():
    """Close the shared proxy HTTP client cleanly."""
    await chatbot.aclose_client()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/ready")
def ready():
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    return {"status": "ready"}
