from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.user import User
from app.schemas.auth import SignupRequest, LoginRequest, TokenResponse
from app.core.security import (
    hash_password, verify_password, generate_api_key, create_access_token,
)

from datetime import timedelta
from app.config import get_settings
from app.models.otp import OTPVerification
from app.core.security import generate_otp
from app.core.email import send_otp_email
from app.schemas.auth import OTPVerifyRequest, OTPVerifyResponse
router = APIRouter(tags=["auth"])
settings = get_settings()


@router.post("/signup", status_code=status.HTTP_201_CREATED)
def signup(body: SignupRequest, db: Session = Depends(get_db)):
    if db.query(User).filter(User.email == body.email).first():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email already registered"
        )
    user = User(
        email=body.email,
        password=hash_password(body.password),
        api_key=generate_api_key(),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    
    # Generate and send OTP
    from datetime import datetime, timezone
    otp_code = generate_otp()
    otp = OTPVerification(
        user_id=user.user_id,
        otp_code=otp_code,
        purpose="signup",
        expires_at=datetime.now(timezone.utc) + timedelta(
            minutes=settings.otp_expires_minutes
        ),
    )
    db.add(otp)
    db.commit()
    send_otp_email(user.email, otp_code)

    return {
        "user_id": user.user_id,
        "email": user.email,
        "api_key": user.api_key,
        "message": "Account created. Check your email for OTP verification.",
    }

@router.post("/verify-otp", response_model=OTPVerifyResponse)
def verify_otp(body: OTPVerifyRequest, db: Session = Depends(get_db)):
    """Verify email address using OTP sent during signup."""
    from datetime import datetime, timezone

    user = db.query(User).filter(User.email == body.email).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found.",
        )

    if user.is_verified:
        return OTPVerifyResponse(message="Email already verified.", verified=True)

    otp = db.query(OTPVerification).filter(
        OTPVerification.user_id == user.user_id,
        OTPVerification.otp_code == body.otp_code,
        OTPVerification.purpose == "signup",
        OTPVerification.is_used == False,
        OTPVerification.expires_at > datetime.now(timezone.utc),
    ).first()

    if not otp:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired OTP.",
        )

    otp.is_used = True
    user.is_verified = True
    db.commit()

    return OTPVerifyResponse(message="Email verified successfully.", verified=True)
@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == body.email).first()
    if not user or not verify_password(body.password, user.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials"
        )
    if not user.is_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Email not verified. Please verify your email with the OTP sent during signup."
        )
    user.login_time = datetime.now(timezone.utc)
    db.commit()
    return TokenResponse(
        access_token=create_access_token(user.user_id),
        token_type="bearer",
        api_key=user.api_key,
        expires_in=settings.jwt_expires,
    )
