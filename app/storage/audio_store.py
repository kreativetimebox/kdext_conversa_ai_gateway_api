import os
import re
from pathlib import PurePosixPath

from app.config import get_settings

settings = get_settings()

SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_relative_path(relative_path: str) -> str:
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


def save_audio(relative_path: str, data: bytes) -> str:
    """Saves audio bytes to the local filesystem and returns a URL path."""
    safe_path = _safe_relative_path(relative_path)
    storage_dir = settings.audio_storage_dir
    storage_root = os.path.abspath(storage_dir)
    full_path = os.path.abspath(os.path.join(storage_root, safe_path))

    if os.path.commonpath([storage_root, full_path]) != storage_root:
        raise ValueError("Invalid audio storage path")

    # Ensure parent directory exists
    os.makedirs(os.path.dirname(full_path), exist_ok=True)

    with open(full_path, "wb") as f:
        f.write(data)

    # Return a URL-accessible path
    # In this app, we will serve the storage directory statically under /audio
    return f"/audio/{safe_path}"
