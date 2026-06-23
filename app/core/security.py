import secrets
from datetime import datetime, timedelta, timezone
import bcrypt
from jose import jwt
from app.config import get_settings

settings = get_settings()


def hash_password(password: str) -> str:
    pwd_bytes = password.encode("utf-8")
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(pwd_bytes, salt)
    return hashed.decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    plain_bytes = plain.encode("utf-8")
    hashed_bytes = hashed.encode("utf-8")
    return bcrypt.checkpw(plain_bytes, hashed_bytes)


def generate_api_key() -> str:
    return "sk_live_" + secrets.token_hex(24)


def create_access_token(user_id: int) -> str:
    expire = datetime.now(timezone.utc) + timedelta(seconds=settings.jwt_expires)
    payload = {"sub": str(user_id), "exp": expire.timestamp()}
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")
import random

def generate_otp() -> str:
    """Generate a 6-digit OTP."""
    return str(random.randint(100000, 999999))
