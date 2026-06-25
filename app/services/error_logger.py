"""Error logging service — saves errors to DB."""

import logging
from sqlalchemy.orm import Session
from app.models.error_log import ErrorLog

logger = logging.getLogger(__name__)


def log_error(
    db: Session,
    endpoint: str,
    method: str,
    error_type: str,
    error_message: str,
    status_code: int | None = None,
    user_id: int | None = None,
) -> None:
    """
    Save an error to the error_logs table.
    Never raises — if DB write fails, logs to file only.

    Args:
        db:            Database session
        endpoint:      Route path e.g. "/signup"
        method:        HTTP method e.g. "POST"
        error_type:    Exception class name e.g. "HTTPException"
        error_message: Error detail message
        status_code:   HTTP status code if available
        user_id:       User ID if request was authenticated
    """
    try:
        entry = ErrorLog(
            user_id=user_id,
            endpoint=endpoint,
            method=method,
            error_type=error_type,
            status_code=status_code,
            error_message=str(error_message)[:2000],  # cap at 2000 chars
        )
        db.add(entry)
        db.commit()
    except Exception as exc:
        # Never crash on logging failure
        logger.error("error_log_db_write_failed: %s", str(exc))