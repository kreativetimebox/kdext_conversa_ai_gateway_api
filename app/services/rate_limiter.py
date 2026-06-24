"""Rate limiting service — per user, per endpoint, RPM + RPD."""

from datetime import datetime, timezone
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models.rate_limit import RateLimit

# Limits — matching Sarvam AI approach
RPM_LIMIT = 10   # requests per minute per user per endpoint
RPD_LIMIT = 100  # requests per day per user per endpoint


def check_rate_limit(user_id: int, endpoint: str, db: Session) -> None:
    """
    Check if user has exceeded rate limits for the given endpoint.
    Increments counters if within limits.
    Raises HTTP 429 if limit exceeded.

    Args:
        user_id:  User's primary key
        endpoint: "tts" or "stt"
        db:       Database session
    """
    now = datetime.now(timezone.utc)
    window_minute = now.strftime("%Y-%m-%d-%H-%M")
    window_day = now.strftime("%Y-%m-%d")

    # ── Per-minute check ──────────────────────────────────────
    rpm_record = db.query(RateLimit).filter(
        RateLimit.user_id == user_id,
        RateLimit.endpoint == endpoint,
        RateLimit.window_minute == window_minute,
    ).first()

    if rpm_record:
        if rpm_record.rpm_count >= RPM_LIMIT:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Rate limit exceeded: {RPM_LIMIT} requests per minute allowed for {endpoint}. Try again in the next minute.",
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
    rpd_record = db.query(RateLimit).filter(
        RateLimit.user_id == user_id,
        RateLimit.endpoint == endpoint,
        RateLimit.window_day == window_day,
        RateLimit.window_minute == "day",
    ).first()

    if rpd_record:
        if rpd_record.rpd_count >= RPD_LIMIT:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Rate limit exceeded: {RPD_LIMIT} requests per day allowed for {endpoint}. Try again tomorrow.",
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