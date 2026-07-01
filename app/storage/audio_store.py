"""Audio storage backend — AWS S3 (production) with local disk fallback (development).

Public API
----------
save_audio(relative_path, data, content_type="audio/wav") -> str
    Saves audio bytes and returns a publicly-accessible URL string.

    - When USE_S3_STORAGE=true  → uploads to S3, returns HTTPS URL
    - When USE_S3_STORAGE=false → saves to local disk, returns /audio/... path
"""

import logging
import os
import re
from pathlib import PurePosixPath
from typing import Optional

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _safe_key(relative_path: str) -> str:
    """Sanitise a relative path so it is safe as an S3 key or filesystem path."""
    normalized = relative_path.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("Invalid audio storage path")

    safe_parts = []
    for part in path.parts:
        safe = SAFE_NAME_RE.sub("_", part).strip("._")
        if not safe:
            raise ValueError("Invalid audio storage path")
        safe_parts.append(safe[:120])

    return "/".join(safe_parts)


def _get_s3_client():
    """Build a boto3 S3 client from app settings. boto3 is imported lazily."""
    try:
        import boto3  # noqa: PLC0415 — lazy import, only needed when USE_S3_STORAGE=true
    except ImportError as exc:
        raise RuntimeError(
            "boto3 is required for S3 storage. Run: pip install boto3==1.35.0"
        ) from exc
    return boto3.client(
        "s3",
        region_name=settings.aws_s3_region,
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
    )


# ---------------------------------------------------------------------------
# S3 backend
# ---------------------------------------------------------------------------

def _save_to_s3(
    s3_key: str,
    data: bytes,
    content_type: str = "audio/wav",
) -> str:
    """Upload *data* to S3 and return the public HTTPS URL.

    The bucket must already exist and have a public-read policy (or use
    pre-signed URLs — swap the return value below if you need private buckets).
    """
    bucket = settings.aws_s3_bucket
    region = settings.aws_s3_region

    try:
        from botocore.exceptions import BotoCoreError, ClientError  # noqa: PLC0415
        client = _get_s3_client()
        client.put_object(
            Bucket=bucket,
            Key=s3_key,
            Body=data,
            ContentType=content_type,
        )
        # Return a relative path so the Gateway proxy routes it securely
        url = f"/audio/{s3_key}"
        logger.info("Uploaded audio to S3, returning relative path: %s", url)
        return url

    except (BotoCoreError, ClientError) as exc:
        logger.error("S3 upload failed for key %s: %s", s3_key, exc)
        raise RuntimeError(f"S3 upload failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Local disk backend (development fallback)
# ---------------------------------------------------------------------------

def _save_to_local(relative_path: str, data: bytes) -> str:
    """Save audio bytes to the local filesystem and return a URL path."""
    storage_root = os.path.abspath(settings.audio_storage_dir)
    full_path = os.path.abspath(os.path.join(storage_root, relative_path))

    if os.path.commonpath([storage_root, full_path]) != storage_root:
        raise ValueError("Invalid audio storage path")

    os.makedirs(os.path.dirname(full_path), exist_ok=True)

    with open(full_path, "wb") as f:
        f.write(data)

    return f"/audio/{relative_path}"


# ---------------------------------------------------------------------------
# Public API — called by tts.py and stt.py
# ---------------------------------------------------------------------------

def save_audio(
    relative_path: str,
    data: bytes,
    content_type: Optional[str] = "audio/wav",
) -> str:
    """Save audio bytes and return a publicly-accessible URL.

    Args:
        relative_path: Path relative to the storage root, e.g. ``"tts/abc123.wav"``.
        data: Raw audio bytes to persist.
        content_type: MIME type of the audio (used as the S3 Content-Type header).

    Returns:
        A URL string — either an S3 HTTPS URL or a local ``/audio/...`` path.
    """
    safe_path = _safe_key(relative_path)

    if settings.use_s3_storage:
        return _save_to_s3(safe_path, data, content_type or "audio/wav")

    return _save_to_local(safe_path, data)
