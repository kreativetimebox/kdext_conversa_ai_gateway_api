"""Rate limiting service — per user, per endpoint, RPM + RPD."""

from datetime import datetime, timezone
from threading import Lock

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models.rate_limit import RateLimit

from app.config import get_settings

settings = get_settings()

# ---------------------------------------------------------------------------
# In-memory rate limiting (used for the "llm" endpoint: chat + translate)
#
# check_rate_limit() below is DB-backed, persisted, and correct across
# restarts/multiple workers — appropriate for tts/stt. For chat/translate,
# each call was paying two extra Postgres round-trips (RPM check + RPD
# check) on top of the auth check, and Postgres is in a different AWS
# region than the gateway, so each round-trip is expensive purely from
# network distance, not query cost. An in-memory counter avoids that
# entirely. Trade-off: counters reset on process restart and aren't shared
# across multiple gateway workers/instances — acceptable for a rate limit
# (worst case, a user gets a bit more than their limit right after a
# restart), not acceptable for anything that must be exactly correct.
# ---------------------------------------------------------------------------
_in_memory_counters: dict[tuple[int, str, str], int] = {}
_in_memory_lock = Lock()
_IN_MEMORY_PRUNE_THRESHOLD = 5000


def _prune_in_memory_counters(current_windows: set[str]) -> None:
    """Drop counters for windows that have already ended, so the dict
    doesn't grow forever across a long-running process."""

    stale_keys = [key for key in _in_memory_counters if key[2] not in current_windows]
    for key in stale_keys:
        del _in_memory_counters[key]


def check_rate_limit_in_memory(user_id: int, endpoint: str) -> None:
    """In-memory equivalent of check_rate_limit() — no DB round-trip.

    Raises HTTP 429 if the per-minute or per-day limit is exceeded.
    """
    now = datetime.now(timezone.utc)
    window_minute = now.strftime("%Y-%m-%d-%H-%M")
    window_day = now.strftime("%Y-%m-%d")

    rpm_key = (user_id, endpoint, window_minute)
    rpd_key = (user_id, endpoint, window_day)

    rpm_limit = settings.rate_limit_rpm
    rpd_limit = settings.rate_limit_rpd

    with _in_memory_lock:
        if len(_in_memory_counters) > _IN_MEMORY_PRUNE_THRESHOLD:
            _prune_in_memory_counters({window_minute, window_day})

        rpm_count = _in_memory_counters.get(rpm_key, 0)
        if rpm_count >= rpm_limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Rate limit exceeded: {rpm_limit} requests per minute allowed for {endpoint}. Try again in the next minute.",
            )

        rpd_count = _in_memory_counters.get(rpd_key, 0)
        if rpd_count >= rpd_limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Rate limit exceeded: {rpd_limit} requests per day allowed for {endpoint}. Try again tomorrow.",
            )

        _in_memory_counters[rpm_key] = rpm_count + 1
        _in_memory_counters[rpd_key] = rpd_count + 1


def check_rate_limit(user_id: int, endpoint: str, db: Session) -> None:
    """
    Check if user has exceeded rate limits for the given endpoint.
    Increments counters if within limits.
    Raises HTTP 429 if limit exceeded.

    Args:
        user_id:  User's primary key
        endpoint: "tts" or "stt" or "llm"
        db:       Database session
    """
    now = datetime.now(timezone.utc)
    window_minute = now.strftime("%Y-%m-%d-%H-%M")
    window_day = now.strftime("%Y-%m-%d")

    rpm_limit = settings.rate_limit_rpm
    rpd_limit = settings.rate_limit_rpd

    # Fetch BOTH window records in one query — the DB is cross-region, so two
    # separate SELECTs paid two full network round-trips per request.
    from sqlalchemy import and_, or_
    records = db.query(RateLimit).filter(
        RateLimit.user_id == user_id,
        RateLimit.endpoint == endpoint,
        or_(
            RateLimit.window_minute == window_minute,
            and_(RateLimit.window_day == window_day, RateLimit.window_minute == "day"),
        ),
    ).all()
    rpm_record = next((r for r in records if r.window_minute == window_minute), None)
    rpd_record = next(
        (r for r in records if r.window_minute == "day" and r.window_day == window_day),
        None,
    )

    # ── Per-minute check ──────────────────────────────────────
    if rpm_record:
        if rpm_record.rpm_count >= rpm_limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Rate limit exceeded: {rpm_limit} requests per minute allowed for {endpoint}. Try again in the next minute.",
            )
        rpm_record.rpm_count += 1
    else:
        rpm_record = RateLimit(
            user_id=user_id,
            endpoint=endpoint,
            window_minute=window_minute,
            window_day=window_day,
            rpm_count=1,
            rpd_count=0,
        )
        db.add(rpm_record)

    # ── Per-day check ─────────────────────────────────────────
    if rpd_record:
        if rpd_record.rpd_count >= rpd_limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Rate limit exceeded: {rpd_limit} requests per day allowed for {endpoint}. Try again tomorrow.",
            )
        rpd_record.rpd_count += 1
    else:
        rpd_record = RateLimit(
            user_id=user_id,
            endpoint=endpoint,
            window_minute="day",
            window_day=window_day,
            rpm_count=0,
            rpd_count=1,
        )
        db.add(rpd_record)

    db.commit()