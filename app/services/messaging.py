"""Messaging service — generation, review lifecycle, and sending.

Flow:
  generate_message()  → OutboundMessage(status=draft)
  approve_message()   → OutboundMessage(status=approved)
  reject_message()    → OutboundMessage(status=rejected)
  send_message()      → OutboundMessage(status=sent | failed)

LinkedIn: direct API sending is not available for personal accounts.
          The service generates the body; the human copies it manually.
          Calling send_message() on a linkedin message raises ValueError.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import Settings
from app.integrations.email_senders import DryRunEmailError, EmailSendError, build_email_sender
from app.integrations.message_providers import MessageContext, build_message_provider
from app.models import (
    Application,
    ApplicationEvent,
    ApplicationStatus,
    Company,
    Contact,
    MessageChannel,
    MessageStatus,
    OutboundMessage,
    ProfessionalEmail,
)
from app.schemas import MessageGenerateRequest, MessagePatch


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _add_event(
    db: Session,
    application: Application,
    event_type: str,
    channel: str | None = None,
    notes: str | None = None,
) -> None:
    db.add(
        ApplicationEvent(
            application_id=application.id,
            event_type=event_type,
            channel=channel,
            notes=notes,
        )
    )


def _pick_best_contact(db: Session, company: Company) -> Contact | None:
    """Return the contact with the highest email confidence for the company."""
    return db.scalar(
        select(Contact)
        .where(Contact.company_id == company.id)
        .where(Contact.professional_email.is_not(None))
        .order_by(Contact.email_confidence.desc().nullslast())
        .limit(1)
    )


def _has_known_recipient(db: Session, application: Application, contact_id: int | None = None) -> bool:
    """Check if a valid recipient email exists for this application / contact / company."""
    if contact_id:
        contact = db.get(Contact, contact_id)
        if contact and contact.professional_email:
            return True
    if application.professional_email:
        return True
    if application.company_id:
        best = db.scalar(
            select(ProfessionalEmail)
            .where(ProfessionalEmail.company_id == application.company_id)
            .order_by(ProfessionalEmail.confidence.desc().nullslast())
            .limit(1)
        )
        if best and best.address:
            return True
    return False


def _resolve_recipient_email(db: Session, message: OutboundMessage) -> str | None:
    """Find the best recipient email for a message."""
    # 1. Contact's email
    if message.contact_id:
        contact = db.get(Contact, message.contact_id)
        if contact and contact.professional_email:
            return contact.professional_email

    # 2. Application's manually set email
    application = db.get(Application, message.application_id)
    if application and application.professional_email:
        return application.professional_email

    # 3. Best discovered email for the company
    if application and application.company_id:
        best = db.scalar(
            select(ProfessionalEmail)
            .where(ProfessionalEmail.company_id == application.company_id)
            .order_by(ProfessionalEmail.confidence.desc().nullslast())
            .limit(1)
        )
        if best:
            return best.address

    return None


def _detect_language(
    application: Application,
    requested_language: str | None = None,
    default_language: str = "fr",
) -> str:
    """Determine message language: explicit request > job text detection > default."""
    if requested_language:
        lang = requested_language.strip().lower()
        if lang in ("en", "english"):
            return "en"
        if lang in ("fr", "french"):
            return "fr"

    # Heuristic detection from position and description
    text = f"{application.position or ''} {application.description or ''}".lower()
    if text.strip():
        en_indicators = [
            "internship", "intern", "software engineer", "developer", "requirements",
            "qualifications", "responsibilities", "looking for", "we offer",
            "skills", "experience", "full-time", "degree in",
        ]
        fr_indicators = [
            "stage", "stagiaire", "fin d'études", "pfe", "ingénieur", "développeur",
            "profil recherché", "missions", "compétences", "nous recherchons",
            "bac+5", "formation", "durée",
        ]
        en_score = sum(1 for kw in en_indicators if kw in text)
        fr_score = sum(1 for kw in fr_indicators if kw in text)

        if en_score > fr_score and en_score >= 2:
            return "en"
        if fr_score > en_score and fr_score >= 2:
            return "fr"

    return default_language if default_language in ("fr", "en") else "fr"


def select_best_candidate_project(
    job_title: str,
    job_description: str | None,
    job_tech_signals: list[str],
    company_tech_signals: list[str],
    candidate_projects: list[dict],
) -> tuple[dict | None, list[str]]:
    """Determines the most relevant candidate project based on technology & keyword overlap."""
    if not candidate_projects:
        return None, []

    combined_signals: set[str] = set(job_tech_signals) | set(company_tech_signals)
    text = f"{job_title} {job_description or ''}".lower()

    best_project: dict | None = None
    best_overlap: list[str] = []
    max_score = -1

    for project in candidate_projects:
        proj_techs = project.get("technologies", [])
        overlap: list[str] = []
        score = 0

        for tech in proj_techs:
            tech_lower = tech.lower()
            if any(s.lower() == tech_lower for s in combined_signals):
                overlap.append(tech)
                score += 3
            elif tech_lower in text:
                overlap.append(tech)
                score += 1

        if score > max_score:
            max_score = score
            best_project = project
            best_overlap = overlap

    if not best_project or max_score <= 0:
        best_project = candidate_projects[0]
        best_overlap = best_project.get("technologies", [])[:3]

    return best_project, best_overlap


def _build_context(
    db: Session,
    application: Application,
    company: Company | None,
    contact: Contact | None,
    settings: Settings,
    channel: str,
    language: str | None = None,
) -> MessageContext:
    from app.integrations.research_providers import extract_technology_signals
    from app.services.research import classify_contact_relevance

    resolved_language = _detect_language(
        application,
        requested_language=language,
        default_language=settings.message_default_language,
    )
    cv_summary = settings.get_cv_summary(resolved_language)

    company_meta = company.metadata_json or {} if company else {}
    company_tech = company_meta.get("technology_signals", [])
    job_tech = extract_technology_signals(f"{application.position} {application.description or ''}")

    candidate_projects = settings.get_candidate_projects()
    selected_proj, personalization_signals = select_best_candidate_project(
        application.position,
        application.description,
        job_tech,
        company_tech,
        candidate_projects,
    )

    contact_rel, contact_reason = classify_contact_relevance(contact.job_title if contact else application.recruiter)

    # --- Phase 3.6.1: follow-up context ---
    # Determine how many follow-ups have already been sent and retrieve
    # the body of the most recently SENT message for this application/channel.
    # Only SENT messages qualify — drafts, approved, rejected, and failed are excluded.
    follow_up_count = application.follow_up_count or 0
    previous_body: str | None = None
    if application.id is not None:
        prev_msg = db.scalar(
            select(OutboundMessage)
            .where(OutboundMessage.application_id == application.id)
            .where(OutboundMessage.channel == channel)
            .where(OutboundMessage.status == MessageStatus.sent)
            .order_by(OutboundMessage.sent_at.desc().nullslast(), OutboundMessage.created_at.desc())
            .limit(1)
        )
        if prev_msg:
            previous_body = prev_msg.body

    return MessageContext(
        candidate_name=settings.candidate_name,
        candidate_degree=settings.candidate_degree or "Master",
        candidate_school=settings.candidate_school,
        candidate_specialization=settings.candidate_specialization,
        candidate_email=settings.candidate_email,
        candidate_cv_summary=cv_summary,
        candidate_skills=settings.candidate_skills,
        candidate_github_url=settings.candidate_github_url,
        candidate_linkedin_url=settings.candidate_linkedin_url,
        candidate_portfolio_url=settings.candidate_portfolio_url,
        job_title=application.position,
        job_description=application.description,
        job_url=application.job_url,
        job_location=application.location,
        job_tech_signals=job_tech,
        company_name=company.name if company else application.company,
        company_description=company.description if company else None,
        company_website=company.website if company else None,
        company_industry=company_meta.get("industry"),
        company_tech_signals=company_tech,
        recruiter_name=contact.name if contact else application.recruiter,
        recruiter_title=contact.job_title if contact else None,
        recruiter_email=contact.professional_email if contact else application.professional_email,
        contact_relevance=contact_rel,
        contact_source=contact.source if contact else None,
        selected_project_name=selected_proj.get("name") if selected_proj else None,
        selected_project_description=selected_proj.get("description") if selected_proj else None,
        personalization_signals=personalization_signals,
        channel=channel,
        follow_up_count=follow_up_count,
        previous_body=previous_body,
        language=resolved_language,
        candidate_cv_summary_fr=settings.candidate_cv_summary_fr,
        candidate_cv_summary_en=settings.candidate_cv_summary_en,
    )



# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def generate_message(
    db: Session,
    application: Application,
    request: MessageGenerateRequest,
    settings: Settings,
) -> OutboundMessage:
    """Generate an outreach message and persist it as a draft."""
    # Pre-flight check: ensure recipient exists for email channel
    if request.channel == MessageChannel.email and not _has_known_recipient(db, application, request.contact_id):
        raise ValueError("No professional recipient email found. Run Research first.")

    # Resolve company and contact
    company: Company | None = (
        db.get(Company, application.company_id) if application.company_id else None
    )
    contact: Contact | None = None
    if request.contact_id:
        contact = db.get(Contact, request.contact_id)
        if not contact:
            raise ValueError(f"Contact {request.contact_id} not found.")
    elif company:
        contact = _pick_best_contact(db, company)

    # Build generation context
    ctx = _build_context(
        db,
        application,
        company,
        contact,
        settings,
        request.channel.value,
        language=request.language,
    )

    # Generate via the requested provider
    try:
        provider = build_message_provider(request.provider, settings)
        generated = await provider.generate(ctx)
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"Message generation failed: {exc}") from exc

    metadata_json = {
        "opportunity_id": application.id,
        "company_id": application.company_id,
        "contact_id": contact.id if contact else None,
        "language": ctx.language,
        "channel": ctx.channel,
        "selected_project": ctx.selected_project_name,
        "personalization_signals": ctx.personalization_signals,
        "contact_relevance": ctx.contact_relevance,
        "provider": generated.provider,
        "generated_at": _utcnow().isoformat(),
        "generation_version": "3.5.3",
    }

    # Persist draft
    message = OutboundMessage(
        application_id=application.id,
        contact_id=contact.id if contact else None,
        channel=request.channel,
        subject=generated.subject,
        body=generated.body,
        generation_provider=generated.provider,
        generation_model=generated.model,
        status=MessageStatus.draft,
        metadata_json=metadata_json,
    )
    db.add(message)
    db.flush()

    # Timeline event
    _add_event(
        db,
        application,
        "message_generated",
        channel=request.channel.value,
        notes=f"Provider: {generated.provider}, message ID: {message.id}",
    )

    db.commit()
    db.refresh(message)
    return message



def edit_message(db: Session, message: OutboundMessage, patch: MessagePatch) -> OutboundMessage:
    """Apply edits to a draft or approved message (resets approval to draft)."""
    changed = False
    if patch.subject is not None:
        message.subject = patch.subject
        changed = True
    if patch.body is not None:
        message.body = patch.body
        changed = True
    if changed and message.status == MessageStatus.approved:
        # Editing after approval resets to draft so it must be re-approved
        message.status = MessageStatus.draft
        message.approved_at = None
    db.commit()
    db.refresh(message)
    return message


def approve_message(db: Session, message: OutboundMessage) -> OutboundMessage:
    """Mark a draft message as approved for sending."""
    if message.status not in (MessageStatus.draft,):
        raise ValueError(
            f"Only draft messages can be approved. Current status: {message.status.value}"
        )
    message.status = MessageStatus.approved
    message.approved_at = _utcnow()
    application = db.get(Application, message.application_id)
    _add_event(
        db,
        application,
        "message_approved",
        channel=message.channel.value,
        notes=f"Message ID: {message.id}",
    )
    db.commit()
    db.refresh(message)
    return message


def reject_message(db: Session, message: OutboundMessage, notes: str | None = None) -> OutboundMessage:
    """Discard a draft message."""
    if message.status not in (MessageStatus.draft, MessageStatus.approved):
        raise ValueError(
            f"Cannot reject a message with status: {message.status.value}"
        )
    message.status = MessageStatus.rejected
    application = db.get(Application, message.application_id)
    _add_event(
        db,
        application,
        "message_rejected",
        channel=message.channel.value,
        notes=notes or f"Message ID: {message.id}",
    )
    db.commit()
    db.refresh(message)
    return message


async def send_message(
    db: Session,
    message: OutboundMessage,
    settings: Settings,
    to_email: str | None = None,
) -> OutboundMessage:
    """Send an approved message and record the outcome."""
    if message.status != MessageStatus.approved:
        raise ValueError(
            f"Only approved messages can be sent. Current status: {message.status.value}"
        )
    if message.status == MessageStatus.sent:
        raise ValueError("This message has already been sent.")

    if message.channel == MessageChannel.linkedin:
        raise ValueError(
            "LinkedIn direct sending is not available via the official API for personal accounts. "
            "Use the review UI to copy the message and send it manually via LinkedIn."
        )

    application = db.get(Application, message.application_id)
    recipient = to_email or _resolve_recipient_email(db, message)
    if not recipient:
        raise ValueError(
            "No recipient email found. Specify to_email or run company research to discover contacts."
        )

    sender = build_email_sender(settings)
    from_addr = settings.smtp_from or settings.smtp_user or "noreply@pfe-automation.local"
    subject = message.subject or f"Candidature Stage PFE – {application.position}"

    try:
        await sender.send(
            to=recipient,
            subject=subject,
            body=message.body,
            from_addr=from_addr,
        )
        message.status = MessageStatus.sent
        message.sent_at = _utcnow()
        message.failure_reason = None
        # Update application tracking
        application.contact_date = application.contact_date or date.today()
        # Phase 3.6.2: transition any pre-contact status to CONTACTED on successful send.
        # Only applies when the message is actually delivered — not on dry-run or SMTP failure.
        _PRE_CONTACT_STATUSES = {
            ApplicationStatus.discovered,
            ApplicationStatus.qualified,
            ApplicationStatus.researched,
            ApplicationStatus.contact_ready,
        }
        if application.application_status in _PRE_CONTACT_STATUSES:
            application.application_status = ApplicationStatus.contacted
        _add_event(
            db,
            application,
            "email_sent",
            channel="email",
            notes=f"To: {recipient} | Subject: {subject} | Provider: {sender.name}",
        )
    except DryRunEmailError as exc:
        # Keep status as approved — do NOT mark as failed for intentional dry-run skips
        message.failure_reason = "[DRY RUN] Email not sent — dry_run_email=True"
        _add_event(
            db,
            application,
            "email_dry_run_skipped",
            channel="email",
            notes=str(exc),
        )
    except EmailSendError as exc:
        message.status = MessageStatus.failed
        message.failure_reason = str(exc)
        _add_event(
            db,
            application,
            "email_failed",
            channel="email",
            notes=str(exc),
        )

    db.commit()
    db.refresh(message)
    return message
