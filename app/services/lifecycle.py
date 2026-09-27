"""Application lifecycle service.

Covers:
- Status transitions with timeline events
- Follow-up scheduling (configurable delays, duplicate prevention)
- Response recording (manual or IMAP-ingested)
- Response confirmation
- Follow-up due queries
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import Settings
from app.models import (
    Application,
    ApplicationEvent,
    ApplicationStatus,
    IncomingResponse,
    OutboundMessage,
    ResponseStatus,
)
from app.schemas import RecordResponseRequest

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_VALID_TRANSITIONS: dict[ApplicationStatus, set[ApplicationStatus]] = {
    ApplicationStatus.discovered:      {ApplicationStatus.qualified, ApplicationStatus.applied, ApplicationStatus.withdrawn},
    ApplicationStatus.qualified:       {ApplicationStatus.researched, ApplicationStatus.applied, ApplicationStatus.withdrawn},
    ApplicationStatus.researched:      {ApplicationStatus.contact_ready, ApplicationStatus.applied, ApplicationStatus.withdrawn},
    ApplicationStatus.contact_ready:   {ApplicationStatus.contacted, ApplicationStatus.applied, ApplicationStatus.withdrawn},
    ApplicationStatus.contacted:       {ApplicationStatus.waiting_response, ApplicationStatus.applied, ApplicationStatus.withdrawn},
    ApplicationStatus.applied:         {ApplicationStatus.waiting_response, ApplicationStatus.withdrawn},
    ApplicationStatus.waiting_response:{ApplicationStatus.follow_up_due, ApplicationStatus.responded, ApplicationStatus.rejected, ApplicationStatus.withdrawn},
    ApplicationStatus.follow_up_due:   {ApplicationStatus.waiting_response, ApplicationStatus.responded, ApplicationStatus.rejected, ApplicationStatus.withdrawn},
    ApplicationStatus.responded:       {ApplicationStatus.interview, ApplicationStatus.rejected, ApplicationStatus.offer, ApplicationStatus.waiting_response, ApplicationStatus.withdrawn},
    ApplicationStatus.interview:       {ApplicationStatus.offer, ApplicationStatus.rejected, ApplicationStatus.withdrawn},
    ApplicationStatus.offer:           {ApplicationStatus.closed, ApplicationStatus.withdrawn},
    ApplicationStatus.rejected:        {ApplicationStatus.withdrawn},
    ApplicationStatus.withdrawn:       set(),
    ApplicationStatus.closed:          set(),
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _add_event(
    db: Session,
    application: Application,
    event_type: str,
    notes: str | None = None,
    channel: str | None = None,
) -> None:
    db.add(ApplicationEvent(
        application_id=application.id,
        event_type=event_type,
        channel=channel,
        notes=notes,
    ))


# ---------------------------------------------------------------------------
# Status transitions
# ---------------------------------------------------------------------------

def transition_status(
    db: Session,
    application: Application,
    new_status: ApplicationStatus,
    notes: str | None = None,
) -> Application:
    """Move an application to a new lifecycle status and record the event.

    Raises ValueError if the transition is not in the allowed map.
    Always allowed (escape hatch): same status as current.
    """
    current = application.application_status
    if current == new_status:
        return application  # no-op

    allowed = _VALID_TRANSITIONS.get(current, set())
    if new_status not in allowed:
        raise ValueError(
            f"Invalid transition {current.value} → {new_status.value}. "
            f"Allowed: {[s.value for s in allowed]}"
        )

    application.application_status = new_status
    if new_status == ApplicationStatus.applied:
        application.application_date = application.application_date or date.today()

    _add_event(
        db, application,
        event_type="status_changed",
        notes=f"{current.value} → {new_status.value}" + (f": {notes}" if notes else ""),
    )
    db.commit()
    db.refresh(application)
    return application


# ---------------------------------------------------------------------------
# Follow-up scheduling
# ---------------------------------------------------------------------------

def schedule_follow_up(
    db: Session,
    application: Application,
    settings: Settings,
) -> Application:
    """Schedule the next follow-up date based on follow-up count and delay config.

    Prevents scheduling a second follow-up if one is already pending.
    Returns the application unchanged if max follow-ups reached.
    """
    if application.follow_up_count >= settings.followup_max:
        raise ValueError(
            f"Maximum follow-ups ({settings.followup_max}) already reached for this application."
        )

    # Prevent duplicate scheduling: if status is waiting_response or follow_up_due
    # and a future/today date is already set, skip
    if (
        application.application_status in (ApplicationStatus.waiting_response, ApplicationStatus.follow_up_due)
        and application.next_follow_up is not None
        and application.next_follow_up >= date.today()
    ):
        raise ValueError(
            f"A follow-up is already scheduled for {application.next_follow_up}."
        )

    delay = (
        settings.followup_delay_days_1
        if application.follow_up_count == 0
        else settings.followup_delay_days_2
    )
    ref_date = application.last_contact or date.today()
    next_due = ref_date + timedelta(days=delay)

    application.next_follow_up = next_due
    application.application_status = ApplicationStatus.waiting_response

    _add_event(
        db, application,
        event_type="follow_up_scheduled",
        notes=f"Follow-up #{application.follow_up_count + 1} due {next_due} (delay: {delay}d)",
    )
    db.commit()
    db.refresh(application)
    return application


def mark_follow_up_sent(
    db: Session,
    application: Application,
) -> Application:
    """Mark a follow-up as sent: increment counter and move back to waiting_response."""
    application.follow_up_count += 1
    application.last_contact = date.today()
    application.next_follow_up = None
    application.application_status = ApplicationStatus.waiting_response
    _add_event(
        db, application,
        event_type="follow_up_sent",
        notes=f"Follow-up #{application.follow_up_count} sent",
    )
    db.commit()
    db.refresh(application)
    return application


def get_due_follow_ups(db: Session) -> list[Application]:
    """Return all applications with a follow-up due today or overdue."""
    return list(db.scalars(
        select(Application)
        .where(Application.next_follow_up <= date.today())
        .where(Application.application_status.in_([
            ApplicationStatus.waiting_response,
            ApplicationStatus.follow_up_due,
        ]))
        .order_by(Application.next_follow_up)
    ))


def get_awaiting_response(db: Session) -> list[Application]:
    """Return all applications currently waiting for a response (no follow-up due yet)."""
    return list(db.scalars(
        select(Application)
        .where(Application.application_status == ApplicationStatus.waiting_response)
        .where(
            (Application.next_follow_up == None) |  # noqa: E711
            (Application.next_follow_up > date.today())
        )
        .order_by(Application.last_contact)
    ))


def mark_follow_ups_due(db: Session) -> int:
    """Scan applications and flag any that have reached their next_follow_up date.

    Returns the count of applications updated. Intended for a daily cron/manual trigger.
    """
    due = list(db.scalars(
        select(Application)
        .where(Application.next_follow_up <= date.today())
        .where(Application.application_status == ApplicationStatus.waiting_response)
    ))
    for app in due:
        app.application_status = ApplicationStatus.follow_up_due
        _add_event(db, app, event_type="follow_up_due", notes=f"Due: {app.next_follow_up}")
    if due:
        db.commit()
    return len(due)


# ---------------------------------------------------------------------------
# Response recording
# ---------------------------------------------------------------------------

def record_response(
    db: Session,
    application: Application,
    request: RecordResponseRequest,
) -> IncomingResponse:
    """Record an incoming response (from IMAP or manual entry).

    Deduplicates by message_id_header when provided.
    If in_reply_to_header is provided, deterministically auto-matches application by OutboundMessage.sent_message_id.
    """
    # Phase 3.6.3: Deterministic In-Reply-To auto-match to application
    if request.in_reply_to_header:
        matched_outbound = db.scalar(
            select(OutboundMessage)
            .where(OutboundMessage.sent_message_id == request.in_reply_to_header)
            .limit(1)
        )
        if matched_outbound:
            app_match = db.get(Application, matched_outbound.application_id)
            if app_match:
                application = app_match

    # Dedup check
    if request.message_id_header:
        existing = db.scalar(
            select(IncomingResponse)
            .where(IncomingResponse.message_id_header == request.message_id_header)
        )
        if existing:
            return existing  # already ingested

    received = request.received_at or _utcnow()
    response = IncomingResponse(
        application_id=application.id,
        received_at=received,
        sender=request.sender,
        subject=request.subject,
        body_preview=request.body_preview[:1000] if request.body_preview else None,
        classification=request.classification,
        confidence=request.confidence,
        source=request.source,
        confirmed=request.confirmed,
        message_id_header=request.message_id_header,
        in_reply_to_header=request.in_reply_to_header,
    )
    db.add(response)
    db.flush()

    _add_event(
        db, application,
        event_type="response_received",
        notes=f"From: {request.sender} | Subject: {request.subject or 'N/A'} | Source: {request.source}",
    )

    # If already confirmed and classified, apply immediately
    if request.confirmed and request.classification:
        _apply_classification(db, application, request.classification, received)

    db.commit()
    db.refresh(response)
    return response


def confirm_response(
    db: Session,
    response: IncomingResponse,
    classification: ResponseStatus,
) -> IncomingResponse:
    """Human confirms the classification of an incoming response."""
    response.classification = classification
    response.confirmed = True
    application = db.get(Application, response.application_id)
    _apply_classification(db, application, classification, response.received_at)

    _add_event(
        db, application,
        event_type="response_classified",
        notes=f"Classification: {classification.value} (confirmed)",
    )
    db.commit()
    db.refresh(response)
    return response


def _apply_classification(
    db: Session,
    application: Application,
    classification: ResponseStatus,
    received_at: datetime,
) -> None:
    """Apply a confirmed classification to the application model."""
    application.response_status = classification
    application.last_response_date = received_at.date() if isinstance(received_at, datetime) else received_at

    # Map classification → application status
    status_map = {
        ResponseStatus.positive:  ApplicationStatus.responded,
        ResponseStatus.negative:  ApplicationStatus.rejected,
        ResponseStatus.interview: ApplicationStatus.interview,
        ResponseStatus.neutral:   ApplicationStatus.responded,
        ResponseStatus.info:      ApplicationStatus.responded,
        ResponseStatus.other:     ApplicationStatus.responded,
    }
    new_app_status = status_map.get(classification)
    if new_app_status and application.application_status not in (
        ApplicationStatus.interview, ApplicationStatus.offer, ApplicationStatus.closed,
    ):
        application.application_status = new_app_status
