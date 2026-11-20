"""Send the sign-in code through the platform mailer, never a tenant mailbox."""

import json
import os
import smtplib
import urllib.error
import urllib.request
from email.message import EmailMessage

from gmail_auto.errors import ConfigError


def deliver_code(to: str, code: str) -> None:
    subject = "Your Mail Autopilot sign-in code"
    body = f"Your sign-in code: {code}\n\nIt works for 10 minutes. If you did not ask for it, ignore this email."
    if _smtp_ready():
        _send_smtp(to, subject, body)
        return
    if os.getenv("RESEND_API_KEY", "").strip():
        sender = os.getenv("MAIL_FROM", "").strip()
        if not sender:
            raise ConfigError("Set MAIL_FROM to the address that sends sign-in codes.")
        _send_resend(to, sender, subject, body)
        return
    raise ConfigError(
        "The code could not be sent. Set SMTP_USER and SMTP_PASSWORD, or RESEND_API_KEY and MAIL_FROM."
    )


def _smtp_ready() -> bool:
    return bool(os.getenv("SMTP_USER", "").strip() and os.getenv("SMTP_PASSWORD", "").strip())


def _send_smtp(to: str, subject: str, body: str) -> None:
    host = os.getenv("SMTP_HOST", "smtp.gmail.com").strip() or "smtp.gmail.com"
    port = int(os.getenv("SMTP_PORT", "587") or "587")
    user = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_PASSWORD", "").strip()
    sender = os.getenv("MAIL_FROM", "").strip() or user
    message = EmailMessage()
    message["To"] = to
    message["From"] = sender
    message["Subject"] = subject
    message.set_content(body)
    try:
        with smtplib.SMTP(host, port, timeout=20) as smtp:
            smtp.starttls()
            smtp.login(user, password)
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        raise ConfigError("The code was not sent. Check the SMTP account and password.") from exc


def _send_resend(to: str, sender: str, subject: str, body: str) -> None:
    key = os.getenv("RESEND_API_KEY", "").strip()
    payload = json.dumps({"from": sender, "to": [to], "subject": subject, "text": body}).encode("utf-8")
    request = urllib.request.Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json", "User-Agent": "autogmail"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            if getattr(response, "status", 200) >= 300:
                raise ConfigError("The code was not sent. Check the Resend account.")
    except ConfigError:
        raise
    except (OSError, urllib.error.URLError) as exc:
        raise ConfigError("The code was not sent. Check the Resend account.") from exc
