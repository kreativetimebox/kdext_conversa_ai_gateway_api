from sqlalchemy.orm import Session
from app.models.user import User

def increment_success(user_id: int, db: Session):
    user = db.query(User).filter(User.user_id == user_id).first()
    if user:
        user.total_processing += 1

def increment_failure(user_id: int, db: Session):
    user = db.query(User).filter(User.user_id == user_id).first()
    if user:
        user.total_failed += 1
