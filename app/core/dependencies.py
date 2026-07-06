from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session
from jose import jwt, JWTError
from app.core.ttl_cache import TTLCache
from app.database import get_db
from app.models.user import User
from app.config import get_settings

settings = get_settings()
bearer_scheme = HTTPBearer()


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    token = credentials.credentials
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
        user_id = int(payload.get("sub"))
    except (JWTError, ValueError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token"
        )
    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found"
        )
    return user


def verify_api_key(x_api_key: str = Header(...),
                   db: Session = Depends(get_db)) -> User:
    user = db.query(User).filter(User.api_key == x_api_key).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key"
        )
    return user


# ── Cached API-key auth ───────────────────────────────────────────────────────
# Postgres is in a different AWS region than this gateway, so the key lookup in
# verify_api_key() costs a full cross-region round-trip on EVERY request. For
# hot paths that only need the caller's user_id (job submits and the /jobs
# polling loop, which fires every second or so per pending job), a short TTL
# cache removes that cost. Same pattern/trade-off as the LLM-proxy router:
# a just-revoked key stays usable for up to TTL seconds.

@dataclass(slots=True)
class CachedUser:
    """Minimal stand-in for User — only .user_id is read on the cached path."""
    user_id: int


_api_key_user_cache: TTLCache[int] = TTLCache(ttl_seconds=60)


def verify_api_key_cached(x_api_key: str = Header(...),
                          db: Session = Depends(get_db)) -> "User | CachedUser":
    cached_user_id = _api_key_user_cache.get(x_api_key)
    if cached_user_id is not None:
        return CachedUser(user_id=cached_user_id)
    user = verify_api_key(x_api_key=x_api_key, db=db)
    _api_key_user_cache.set(x_api_key, user.user_id)
    return user