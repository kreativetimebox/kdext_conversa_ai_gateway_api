"""User profile response schema."""

from datetime import datetime

from pydantic import BaseModel


class ProfileResponse(BaseModel):
    """GET /profile response body."""

    model_config = {"from_attributes": True}

    user_id: int
    email: str
    api_key: str
    login_time: datetime | None = None
    signout_time: datetime | None = None
    total_processing: int = 0
    total_failed: int = 0
