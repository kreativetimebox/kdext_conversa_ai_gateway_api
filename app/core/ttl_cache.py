"""Tiny in-process TTL cache.

Used to avoid re-querying Postgres for data that rarely changes (e.g. API-key
validity) on every single request/message — the DB round-trip is expensive
here specifically because of cross-region network distance, not query cost.

Not distributed and not persisted: each gateway process has its own cache,
and a restart clears it. That's an acceptable trade-off for short-TTL data
like "is this API key currently valid" — worst case, a just-revoked key stays
usable for up to TTL seconds longer than before.
"""

import time
from threading import Lock
from typing import Generic, TypeVar

T = TypeVar("T")


class TTLCache(Generic[T]):
    """A minimal thread-safe TTL cache: get-or-compute with expiry."""

    def __init__(self, ttl_seconds: float) -> None:
        self._ttl = ttl_seconds
        self._store: dict[str, tuple[T, float]] = {}
        self._lock = Lock()

    def get(self, key: str) -> T | None:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            value, expires_at = entry
            if time.monotonic() >= expires_at:
                del self._store[key]
                return None
            return value

    def set(self, key: str, value: T) -> None:
        with self._lock:
            self._store[key] = (value, time.monotonic() + self._ttl)

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)
