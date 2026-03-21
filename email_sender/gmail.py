"""
Sends the HTML newsletter via Gmail SMTP using an App Password.

Setup (one-time, ~2 minutes):
  1. Create a dedicated Gmail account (e.g. stryktipset.tips@gmail.com)
  2. Enable 2-Step Verification on that account
  3. Go to: myaccount.google.com → Security → App Passwords
  4. Select "Mail" → Generate → copy the 16-character password
  5. Add to .env:  GMAIL_SENDER=stryktipset.tips@gmail.com
                   GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx

No Google Cloud Console, no OAuth flows, no token files needed.
"""

from __future__ import annotations

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from config import GMAIL_APP_PASSWORD, GMAIL_SENDER, NEWSLETTER_RECIPIENTS
from utils.logger import get_logger

logger = get_logger(__name__)

_SMTP_HOST = "smtp.gmail.com"
_SMTP_PORT = 465   # SSL — no STARTTLS negotiation needed


def send_newsletter(subject: str, html_body: str, recipients: Optional[list[str]] = None) -> int:
    """
    Send the newsletter to all recipients via Gmail SMTP.
    Returns the number of successfully sent emails.
    """
    targets = recipients or NEWSLETTER_RECIPIENTS
    if not targets:
        logger.error("No recipients configured — set NEWSLETTER_RECIPIENTS in .env")
        return 0

    sent = 0
    try:
        with smtplib.SMTP_SSL(_SMTP_HOST, _SMTP_PORT) as server:
            server.login(GMAIL_SENDER, GMAIL_APP_PASSWORD)
            logger.info("Gmail SMTP login successful as %s", GMAIL_SENDER)

            for recipient in targets:
                msg = MIMEMultipart("alternative")
                msg["Subject"] = subject
                msg["From"] = GMAIL_SENDER
                msg["To"] = recipient
                msg.attach(MIMEText(html_body, "html", "utf-8"))

                server.send_message(msg)
                logger.info("Newsletter sent to %s", recipient)
                sent += 1

    except smtplib.SMTPAuthenticationError:
        logger.error(
            "Gmail authentication failed. Check GMAIL_SENDER and GMAIL_APP_PASSWORD in .env.\n"
            "App Password must be generated at: myaccount.google.com → Security → App Passwords"
        )
    except smtplib.SMTPException as e:
        logger.error("SMTP error sending newsletter: %s", e)

    logger.info("Newsletter delivery complete: %d/%d sent", sent, len(targets))
    return sent
