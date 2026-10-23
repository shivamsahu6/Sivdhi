
"""Sivdhi email OTP sender.

Loads SMTP settings from the project-root .env file.
Never logs OTPs, app passwords, or SMTP authentication payloads.
"""

import logging
import os
import re
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_DIR / ".env"

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_OTP_RE = re.compile(r"^\d{6}$")


def _get_smtp_settings():
    """Read current configuration and normalize Google's grouped app password."""
    # Keep existing environment variables authoritative, as in app.py.
    load_dotenv(ENV_FILE, override=False)

    sender = (os.getenv("EMAIL") or "").strip()

    # Google displays App Passwords in groups.
    # Spaces/newlines copied along with the password are formatting only.
    app_password = re.sub(
        r"\s+",
        "",
        os.getenv("EMAIL_APP_PASSWORD") or "",
    )

    host = (os.getenv("EMAIL_SMTP_HOST") or "smtp.gmail.com").strip()

    try:
        port = int((os.getenv("EMAIL_SMTP_PORT") or "465").strip())
    except (TypeError, ValueError):
        raise RuntimeError(
            "EMAIL_SMTP_PORT must be 465 (SSL) or 587 (STARTTLS)."
        ) from None

    if port not in (465, 587):
        raise RuntimeError(
            "EMAIL_SMTP_PORT must be 465 (SSL) or 587 (STARTTLS)."
        )

    try:
        timeout = int((os.getenv("EMAIL_SMTP_TIMEOUT") or "20").strip())
    except (TypeError, ValueError):
        timeout = 20

    timeout = max(5, min(timeout, 60))

    if not sender or not app_password:
        missing = []
        if not sender:
            missing.append("EMAIL")
        if not app_password:
            missing.append("EMAIL_APP_PASSWORD")

        raise RuntimeError(
            "Email configuration is missing: "
            + ", ".join(missing)
            + ". Check the .env file beside app.py."
        )

    if not _EMAIL_RE.fullmatch(sender):
        raise RuntimeError("EMAIL must be a valid sender email address.")

    if not host or any(ch.isspace() for ch in host):
        raise RuntimeError("EMAIL_SMTP_HOST is invalid.")

    return sender, app_password, host, port, timeout


def _validate_recipient(email):
    recipient = (email or "").strip()

    if not recipient or not _EMAIL_RE.fullmatch(recipient):
        raise ValueError("Invalid recipient email address.")

    return recipient


def send_otp(email, otp):
    """Send a password-reset OTP email."""
    recipient = _validate_recipient(email)
    otp_text = str(otp).strip()

    if not _OTP_RE.fullmatch(otp_text):
        raise ValueError("OTP must be a 6-digit value.")

    sender, app_password, host, port, timeout = _get_smtp_settings()

    message = MIMEMultipart()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = "Sivdhi Password Reset OTP"

    body = (
        "Hello,\n\n"
        "Your Sivdhi password reset OTP is:\n\n"
        f"{otp_text}\n\n"
        "This OTP is valid for 5 minutes.\n"
        "Do not share this OTP with anyone.\n\n"
        "Thanks,\n"
        "Sivdhi Team\n"
    )

    message.attach(MIMEText(body, "plain", "utf-8"))
    server = None
    stage = "connecting"

    try:
        tls_context = ssl.create_default_context()

        if port == 465:
            server = smtplib.SMTP_SSL(
                host,
                port,
                timeout=timeout,
                context=tls_context,
            )
            stage = "SMTP greeting"
            server.ehlo()
        else:
            server = smtplib.SMTP(host, port, timeout=timeout)
            stage = "SMTP greeting"
            server.ehlo()

            stage = "STARTTLS"
            server.starttls(context=tls_context)
            server.ehlo()

        stage = "authentication"
        server.login(sender, app_password)

        stage = "sending message"
        refused = server.sendmail(
            sender,
            [recipient],
            message.as_string(),
        )

        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)

        logger.info("Sivdhi OTP email sent successfully.")
        return True

    except (smtplib.SMTPException, OSError) as exc:
        # Do not log the OTP, app password, or SMTP debug/auth payloads.
        logger.error(
            "Sivdhi OTP email failed at stage '%s' via %s:%s. "
            "Error type: %s; details: %s",
            stage,
            host,
            port,
            type(exc).__name__,
            str(exc),
        )

        raise RuntimeError(
            "Unable to send OTP email. "
            "Check the server log for the failed SMTP stage."
        ) from exc

    finally:
        if server is not None:
            try:
                server.quit()
            except (smtplib.SMTPException, OSError):
                try:
                    server.close()
                except (smtplib.SMTPException, OSError):
                    pass


def send_email(email, subject, body):
    """Send a non-OTP transactional email using the configured SMTP account."""
    recipient = _validate_recipient(email)
    subject_text = str(subject or "Sivdhi Update").strip()[:180]
    body_text = str(body or "").strip()[:12000]
    if not subject_text or not body_text:
        raise ValueError("Email subject and body are required.")

    sender, app_password, host, port, timeout = _get_smtp_settings()
    message = MIMEMultipart()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject_text
    message.attach(MIMEText(body_text, "plain", "utf-8"))
    server = None
    stage = "connecting"
    try:
        tls_context = ssl.create_default_context()
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=timeout, context=tls_context)
            stage = "SMTP greeting"
            server.ehlo()
        else:
            server = smtplib.SMTP(host, port, timeout=timeout)
            stage = "SMTP greeting"
            server.ehlo()
            stage = "STARTTLS"
            server.starttls(context=tls_context)
            server.ehlo()
        stage = "authentication"
        server.login(sender, app_password)
        stage = "sending message"
        refused = server.sendmail(sender, [recipient], message.as_string())
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)
        logger.info("Sivdhi transactional email sent successfully.")
        return True
    except (smtplib.SMTPException, OSError) as exc:
        logger.error(
            "Sivdhi email failed at stage '%s' via %s:%s. Error type: %s; details: %s",
            stage, host, port, type(exc).__name__, str(exc),
        )
        raise RuntimeError("Unable to send email. Check SMTP configuration and server logs.") from exc
    finally:
        if server is not None:
            try:
                server.quit()
            except (smtplib.SMTPException, OSError):
                try:
                    server.close()
                except (smtplib.SMTPException, OSError):
                    pass
