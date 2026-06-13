"""Authentication request/response schemas."""

from pydantic import BaseModel, ConfigDict, Field, field_validator


def validate_email(value: str) -> str:
    """Validate enough email shape for API input without extra dependencies."""

    if "@" not in value or value.startswith("@") or value.endswith("@"):
        raise ValueError("Invalid email address")
    local, domain = value.rsplit("@", 1)
    if not local or "." not in domain or domain.startswith(".") or domain.endswith("."):
        raise ValueError("Invalid email address")
    return value.lower()


class SignupRequest(BaseModel):
    """POST /signup request body."""

    model_config = ConfigDict(extra="forbid")

    email: str = Field(..., min_length=5, max_length=255, description="User email")
    password: str = Field(..., min_length=8, max_length=128, description="User password")

    _validate_email = field_validator("email")(validate_email)


class LoginRequest(BaseModel):
    """POST /login request body."""

    model_config = ConfigDict(extra="forbid")

    email: str = Field(..., min_length=5, max_length=255)
    password: str = Field(..., min_length=1, max_length=128)

    _validate_email = field_validator("email")(validate_email)


class TokenResponse(BaseModel):
    """POST /login response body."""

    access_token: str
    token_type: str = "bearer"
    api_key: str
    expires_in: int
