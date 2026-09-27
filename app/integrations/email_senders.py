"""Email sending provider abstraction.

Supported senders:
  - ``smtp``  — Standard SMTP with STARTTLS (Gmail App Passwords, any SMTP server).
  - ``null``  — No-op sender used in tests; logs would-be sends.

SMTP setup for Gmail:
  1. Enable 2-factor authentication on the Gmail account.
  2. Generate an App Password: Google Account → Security → App Passwords.
  3. Set SMTP_USER=your@gmail.com and SMTP_PASSWORD=<app-password>.

Required env vars for SMTP:
  SMTP_USER      — sender email address
  SMTP_PASSWORD  — App Password (Gmail) or SMTP credential
  SMTP_HOST      — defaults to smtp.gmail.com
  SMTP_PORT      — defaults to 587 (STARTTLS)
  SMTP_FROM      — optional display/from address, defaults to SMTP_USER
"""
from __future__ import annotations

import smtplib
from abc import ABC, abstractmethod
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.database import Settings


class EmailSendError(Exception):
    """Raised when an email fails to send; preserves the failure reason."""


class DryRunEmailError(EmailSendError):
    """Raised specifically when an intentional dry-run blocks delivery.

    This is a *subclass* of EmailSendError so existing catch-all handlers
    remain safe, but callers can distinguish a dry-run skip from a real
    SMTP failure by catching DryRunEmailError first.
    """


class EmailSender(ABC):
    name: str

    @abstractmethod
    async def send(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        from_addr: str,
        message_id: str | None = None,
        in_reply_to: str | None = None,
        references: str | None = None,
    ) -> None:
        """Send an email. Raises EmailSendError on failure."""
        raise NotImplementedError


class SMTPEmailSender(EmailSender):
    """Sends via SMTP STARTTLS (works with Gmail App Passwords out of the box)."""

    name = "smtp"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def send(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        from_addr: str,
        message_id: str | None = None,
        in_reply_to: str | None = None,
        references: str | None = None,
    ) -> None:
        if not self.settings.smtp_user or not self.settings.smtp_password:
            raise EmailSendError(
                "SMTP sending requires SMTP_USER and SMTP_PASSWORD. "
                "For Gmail: enable 2FA and create an App Password."
            )
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = from_addr
        msg["To"] = to
        if message_id:
            msg["Message-ID"] = message_id
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
        if references:
            msg["References"] = references
        msg.attach(MIMEText(body, "plain", "utf-8"))
        try:
            with smtplib.SMTP(self.settings.smtp_host, self.settings.smtp_port, timeout=15) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(self.settings.smtp_user, self.settings.smtp_password)
                server.sendmail(from_addr, [to], msg.as_string())
        except smtplib.SMTPException as exc:
            raise EmailSendError(f"SMTP error: {exc}") from exc
        except OSError as exc:
            raise EmailSendError(f"Network error sending email: {exc}") from exc


class NullEmailSender(EmailSender):
    """No-op sender — for tests and missing-credentials graceful degradation."""

    name = "null"
    sent: list[dict]  # captured calls, useful in tests

    def __init__(self) -> None:
        self.sent = []

    async def send(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        from_addr: str,
        message_id: str | None = None,
        in_reply_to: str | None = None,
        references: str | None = None,
    ) -> None:
        self.sent.append({
            "to": to,
            "subject": subject,
            "body": body,
            "from": from_addr,
            "message_id": message_id,
            "in_reply_to": in_reply_to,
            "references": references,
        })


class DryRunEmailSender(EmailSender):
    """Dry-run sender — captures the email but refuses to deliver it.

    Enabled when DRY_RUN_EMAIL=true (the default).  This makes it impossible
    to accidentally send a real email during development or testing.

    Raises ``DryRunEmailError`` (a subclass of ``EmailSendError``) so that
    callers can distinguish an intentional dry-run skip from a real failure.
    """

    name = "dry_run"
    captured: list[dict]

    def __init__(self) -> None:
        self.captured = []

    async def send(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        from_addr: str,
        message_id: str | None = None,
        in_reply_to: str | None = None,
        references: str | None = None,
    ) -> None:
        self.captured.append({
            "to": to,
            "subject": subject,
            "body": body,
            "from": from_addr,
            "message_id": message_id,
            "in_reply_to": in_reply_to,
            "references": references,
        })
        raise DryRunEmailError(
            "[DRY RUN] Email was NOT sent — dry_run_email is enabled.\n"
            f"Would have sent to: {to} | Subject: {subject}\n"
            "Set DRY_RUN_EMAIL=false in your .env to enable real delivery."
        )


def build_email_sender(settings: Settings) -> EmailSender:
    """Return the appropriate email sender based on settings.

    Priority:
      1. DRY_RUN_EMAIL=true  → DryRunEmailSender (blocks real delivery)
      2. SMTP credentials set → SMTPEmailSender
      3. Fallback             → NullEmailSender (no-op, for tests)
    """
    if settings.dry_run_email:
        return DryRunEmailSender()
    if settings.smtp_user and settings.smtp_password:
        return SMTPEmailSender(settings)
    return NullEmailSender()
