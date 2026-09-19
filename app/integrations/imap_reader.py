"""IMAP-based response ingestion.

Uses Python's built-in imaplib (no extra dependencies) over SSL.
Reuses SMTP_USER + SMTP_PASSWORD credentials — just enable IMAP in Gmail settings.

Gmail setup:
  1. Gmail → Settings → See all settings → Forwarding and POP/IMAP → Enable IMAP
  2. Use SMTP_USER / SMTP_PASSWORD (App Password) — same credentials as sending.

Design:
  - Only reads; never deletes or marks emails.
  - Deduplicates via the email Message-ID header.
  - Returns raw EmailCandidate objects — classification is heuristic only.
  - Human confirmation is required before updating application status.

IMAP is chosen over Gmail API because:
  - No OAuth setup (reuses existing App Password)
  - No Google Cloud project required
  - Standard protocol works with any provider
"""
from __future__ import annotations

import email as email_lib
import imaplib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.header import decode_header
from email.utils import parsedate_to_datetime


@dataclass
class EmailCandidate:
    """A candidate response email fetched from the inbox."""
    message_id: str
    sender: str
    subject: str
    body_preview: str
    received_at: datetime
    classification_hint: str | None = None  # heuristic classification
    confidence: float = 0.0


def _decode_header_value(raw: str | None) -> str:
    if not raw:
        return ""
    parts = decode_header(raw)
    decoded = []
    for part, enc in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(enc or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return " ".join(decoded)


def _classify_heuristic(subject: str, body: str) -> tuple[str | None, float]:
    """Very conservative heuristic — only flags high-confidence signals.

    Returns (classification_value, confidence). Returns (None, 0) when unsure.
    Human confirmation should always be requested.
    """
    text = (subject + " " + body).lower()

    # Positive signals
    positive_patterns = [
        r"\bentretien\b", r"\binterview\b", r"\brencontre\b",
        r"\bdisponibilit[eé]\b", r"\bopportunit[eé]\b",
        r"\bint[eé]ress[eé]\b", r"\bcandidature retenue\b",
    ]
    # Negative signals
    negative_patterns = [
        r"\brefus[eé]\b", r"\bne correspond pas\b", r"\bpas retenu[e]?\b",
        r"\bsans suite\b", r"\bregret", r"\bdoesn.t match\b",
        r"\bnot selected\b", r"\bunsuccessful\b",
    ]
    # Interview invitation
    interview_patterns = [
        r"\bentretien\b", r"\binterview\b", r"\brencontre\b", r"\bdiscussion\b",
    ]

    for pat in interview_patterns:
        if re.search(pat, text):
            return "interview invitation", 0.7

    for pat in positive_patterns:
        if re.search(pat, text):
            return "positive response", 0.6

    for pat in negative_patterns:
        if re.search(pat, text):
            return "negative response", 0.75

    return None, 0.0


def _extract_body(msg: email.message.Message) -> str:  # type: ignore[name-defined]
    """Extract plain text body, truncated to 1000 chars."""
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    body = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                    break
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            body = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
    return body[:1000]


def fetch_replies(
    imap_host: str,
    imap_port: int,
    username: str,
    password: str,
    since_days: int = 30,
    search_term: str | None = None,
) -> list[EmailCandidate]:
    """Connect via IMAP SSL and fetch recent emails from INBOX.

    Args:
        imap_host: IMAP server hostname.
        imap_port: IMAP SSL port (typically 993).
        username: Email address (same as SMTP_USER).
        password: App Password (same as SMTP_PASSWORD).
        since_days: Fetch emails from the past N days.
        search_term: Optional subject keyword to narrow search.

    Returns:
        List of EmailCandidate objects (not persisted).

    Raises:
        RuntimeError: If IMAP credentials are missing or connection fails.
    """
    if not username or not password:
        raise RuntimeError(
            "IMAP response ingestion requires SMTP_USER and SMTP_PASSWORD. "
            "Enable IMAP in Gmail settings and use an App Password."
        )

    from datetime import date, timedelta
    since_date = (date.today() - timedelta(days=since_days)).strftime("%d-%b-%Y")

    try:
        conn = imaplib.IMAP4_SSL(imap_host, imap_port)
        conn.login(username, password)
        conn.select("INBOX", readonly=True)  # readonly: never modifies emails

        if search_term:
            criteria = f'(SINCE {since_date} SUBJECT "{search_term}")'
        else:
            criteria = f"(SINCE {since_date})"

        _, msg_ids_raw = conn.search(None, criteria)
        msg_ids = msg_ids_raw[0].split() if msg_ids_raw[0] else []

        results: list[EmailCandidate] = []
        for msg_id in msg_ids[-100:]:  # limit to last 100 to avoid timeouts
            _, raw = conn.fetch(msg_id, "(RFC822)")
            if not raw or not raw[0]:
                continue
            raw_bytes = raw[0][1] if isinstance(raw[0], tuple) else raw[0]
            msg = email_lib.message_from_bytes(raw_bytes)

            mid = msg.get("Message-ID", "").strip()
            sender = _decode_header_value(msg.get("From", ""))
            subject = _decode_header_value(msg.get("Subject", ""))
            body = _extract_body(msg)

            date_str = msg.get("Date", "")
            try:
                received_at = parsedate_to_datetime(date_str).astimezone(timezone.utc).replace(tzinfo=None)
            except Exception:
                received_at = datetime.utcnow()

            classification, confidence = _classify_heuristic(subject, body)

            results.append(EmailCandidate(
                message_id=mid,
                sender=sender,
                subject=subject,
                body_preview=body,
                received_at=received_at,
                classification_hint=classification,
                confidence=confidence,
            ))

        conn.close()
        conn.logout()
        return results

    except imaplib.IMAP4.error as exc:
        raise RuntimeError(f"IMAP authentication or connection error: {exc}") from exc
    except OSError as exc:
        raise RuntimeError(f"IMAP network error: {exc}") from exc
