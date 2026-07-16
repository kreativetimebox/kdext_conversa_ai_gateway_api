"""Audio storage backend — AWS S3 (production) with local disk fallback (development).

Public API
----------
save_audio(relative_path, data, content_type="audio/wav") -> str
    Saves audio bytes and returns a publicly-accessible URL string.

    - When USE_S3_STORAGE=true  → uploads to S3, returns HTTPS URL
    - When USE_S3_STORAGE=false → saves to local disk, returns /audio/... path
"""

import hashlib
import hmac
import logging
import os
import re
import time
from pathlib import PurePosixPath
from typing import Optional
from urllib.parse import quote, urlencode

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")

# Cached boto3 S3 client — rebuilding it per request is measurable overhead
# (credential/endpoint resolution + TLS setup), so we build it once. Mirrors the
# worker's cached client in kdext_conversa_ai_sqs/worker/storage.py.
_s3_client = None


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
    """Return a cached boto3 S3 client, building it once on first use.

    boto3 is imported lazily so the dependency is only required when S3 storage
    is enabled. The client is cached module-side because rebuilding it per call
    re-resolves credentials/endpoints and re-establishes TLS — pure overhead.
    """
    global _s3_client
    if _s3_client is None:
        try:
            import boto3  # noqa: PLC0415 — lazy import, only needed when USE_S3_STORAGE=true
        except ImportError as exc:
            raise RuntimeError(
                "boto3 is required for S3 storage. Run: pip install boto3==1.35.0"
            ) from exc
        _s3_client = boto3.client(
            "s3",
            region_name=settings.aws_s3_region,
            aws_access_key_id=settings.aws_access_key_id,
            aws_secret_access_key=settings.aws_secret_access_key,
        )
    return _s3_client


def warm_s3_client() -> None:
    """Build the S3 client ahead of first use (avoids cold-start on first presign)."""
    if settings.use_s3_storage:
        _get_s3_client()


def _key_from_audio_url(audio_url: str) -> str:
    """Extract the storage key (e.g. ``tts/225.wav``) from a stored audio_url.

    Stored URLs are relative paths like ``/audio/tts/225.wav``; the storage key
    is the part after the ``/audio/`` prefix.
    """
    path = audio_url.split("?", 1)[0]  # drop any existing query string
    if path.startswith("/audio/"):
        path = path[len("/audio/"):]
    return path.lstrip("/")


def _sign_local_path(key: str, expires_at: int) -> str:
    """HMAC token binding a storage key to an expiry, for the local /audio route.

    Native ``<audio src>`` elements cannot send an Authorization header, so the
    access credential must live in the URL. This mirrors what a presigned S3 URL
    does, but for the gateway's own local-disk serving path.
    """
    msg = f"{key}:{expires_at}".encode("utf-8")
    return hmac.new(settings.jwt_secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def verify_local_audio_token(key: str, expires_at: int, token: str) -> bool:
    """Validate a signed local /audio token (constant-time), rejecting if expired."""
    if expires_at < int(time.time()):
        return False
    expected = _sign_local_path(key, expires_at)
    return hmac.compare_digest(expected, token or "")


def presign_audio_url(
    audio_url: Optional[str],
    expires_in: Optional[int] = None,
    download_name: Optional[str] = None,
) -> Optional[str]:
    """Turn a stored relative audio_url into a short-lived, unguessable URL.

    - S3 mode  → a presigned S3 GET URL (browser fetches direct from S3, so the
      gateway is not in the byte path and the bucket stays private).
    - Local mode → the same ``/audio/...`` path plus an HMAC ``token``/``exp`` the
      local serving handler validates.

    When *download_name* is given, the URL is made to serve with
    ``Content-Disposition: attachment; filename=...`` so a click saves the file
    (with a proper name, no new tab) instead of playing it inline. Omit it for
    the playback URL — ``<audio>`` ignores the header either way, but keeping the
    playback URL clean avoids surprises.

    Callers should invoke this only after confirming the requester owns the job,
    since the returned URL is a bearer credential for its short TTL.
    """
    if not audio_url:
        return audio_url
    if audio_url.startswith("http://") or audio_url.startswith("https://"):
        return audio_url  # already absolute (nothing to presign)

    ttl = expires_in if expires_in is not None else settings.audio_url_ttl_seconds
    key = _key_from_audio_url(audio_url)

    if settings.use_s3_storage:
        try:
            from botocore.exceptions import BotoCoreError, ClientError  # noqa: PLC0415
            client = _get_s3_client()
            params = {"Bucket": settings.aws_s3_bucket, "Key": key}
            if download_name:
                params["ResponseContentDisposition"] = (
                    f'attachment; filename="{download_name}"'
                )
            return client.generate_presigned_url(
                "get_object",
                Params=params,
                ExpiresIn=ttl,
            )
        except (BotoCoreError, ClientError) as exc:
            logger.error("Failed to presign S3 audio key %s: %s", key, exc)
            raise RuntimeError(f"Failed to presign audio URL: {exc}") from exc

    # Local mode: append a signed, expiring token to the /audio path.
    expires_at = int(time.time()) + ttl
    token = _sign_local_path(key, expires_at)
    query = {"exp": expires_at, "token": token}
    if download_name:
        query["dl"] = download_name
    return f"/audio/{quote(key)}?{urlencode(query)}"


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
