"""Gmail SMTP notifications (same as V1)."""
from __future__ import annotations

import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional

from config import GMAIL_USER, GMAIL_APP_PASSWORD, NOTIFY_EMAIL
from logger_setup import get_logger

log = get_logger(__name__)

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587


def send_notification(
    subject: str,
    body: str,
    to: Optional[str] = None,
) -> bool:
    """Send an email notification. Returns True on success."""
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        log.warning("Gmail creds missing — skipping notification: %s", subject)
        return False

    to = to or NOTIFY_EMAIL
    if not to:
        log.warning("No notify email configured — skipping")
        return False

    msg = MIMEMultipart("alternative")
    msg["From"] = GMAIL_USER
    msg["To"] = to
    msg["Subject"] = f"AI-BOT {subject}"
    msg.attach(MIMEText(body, "plain", "utf-8"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.ehlo()
            server.starttls()
            server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            server.send_message(msg)
        log.info("Email sent: %s", subject)
        return True
    except Exception as e:
        log.error("Failed to send email: %s", e)
        return False