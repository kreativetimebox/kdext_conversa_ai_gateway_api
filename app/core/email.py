"""Email sending utility — OTP via SMTP."""

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.config import get_settings


import logging
logger = logging.getLogger(__name__)


def send_otp_email(to_email: str, otp_code: str) -> bool:
    """Send OTP email. Returns True if sent, False if skipped/failed."""
    settings = get_settings()

    if not settings.smtp_user or not settings.smtp_password:
        logger.warning(
            "smtp_not_configured — Set SMTP_USER and SMTP_PASSWORD in .env | OTP: %s",
            otp_code,
        )
       
        return False

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = "Your verification code"
        msg["From"] = settings.email_from
        msg["To"] = to_email

        body = f"""
        <html><body>
        <h2>Verify your email</h2>
        <p>Your verification code is:</p>
        <h1 style="letter-spacing:8px">{otp_code}</h1>
        <p>This code expires in {settings.otp_expires_minutes} minutes.</p>
        <p>If you did not request this, ignore this email.</p>
        </body></html>
        """
        msg.attach(MIMEText(body, "html"))

        with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
            server.starttls()
            server.login(settings.smtp_user, settings.smtp_password)
            server.sendmail(settings.email_from, to_email, msg.as_string())

        
        logger.info("otp_email_sent to %s", to_email)
        return True

    except Exception as exc:
        
        logger.error("otp_email_failed to %s: %s", to_email, str(exc))
        return False