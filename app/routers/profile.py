from fastapi import APIRouter, Depends
from app.core.dependencies import get_current_user
from app.models.user import User
from app.schemas.user import ProfileResponse

router = APIRouter(tags=["profile"])


@router.get("/profile", response_model=ProfileResponse)
def profile(user: User = Depends(get_current_user)):
    return user
