from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import Application, ApplicationEvent
from app.schemas import EventCreate, OpportunityIn
from app.scoring import ScoreResult, score_opportunity


def upsert_opportunity(
    db: Session, opportunity: OpportunityIn, score: ScoreResult | None = None
) -> tuple[Application, bool]:
    normalized_company = opportunity.company.strip().lower()
    normalized_position = opportunity.title.strip().lower()
    normalized_location = (opportunity.location or "").strip().lower()
    clauses = [Application.job_url == str(opportunity.url)]
    if opportunity.external_id:
        clauses.append(
            (Application.source == opportunity.source)
            & (Application.external_id == opportunity.external_id)
        )
    clauses.append(
        (func.lower(Application.company) == normalized_company)
        & (func.lower(Application.position) == normalized_position)
        & (func.lower(func.coalesce(Application.location, "")) == normalized_location)
    )
    query = select(Application).where(or_(*clauses))
    existing = db.scalar(query)
    score = score or score_opportunity(opportunity)

    if existing:
        existing.company = opportunity.company
        existing.position = opportunity.title
        existing.location = opportunity.location
        existing.description = opportunity.description
        existing.posted_at = opportunity.posted_at or existing.posted_at
        existing.recruiter = opportunity.recruiter or existing.recruiter
        existing.recruiter_linkedin_url = (
            str(opportunity.recruiter_linkedin_url)
            if opportunity.recruiter_linkedin_url
            else existing.recruiter_linkedin_url
        )
        existing.professional_email = opportunity.professional_email or existing.professional_email
        
        # Enriched notes with multi-source provenance
        current_notes = existing.notes or ""
        if opportunity.source and opportunity.source != existing.source:
            tag = f"also_discovered_by={opportunity.source}"
            if tag not in current_notes:
                current_notes = f"{current_notes} ; {tag}".strip(" ;")
        if opportunity.notes and opportunity.notes not in current_notes:
            current_notes = f"{current_notes} ; {opportunity.notes}".strip(" ;")
        existing.notes = current_notes or None

        existing.score = score.score
        existing.score_reason = score.reason
        existing.relevance_category = score.relevance_category
        return existing, False

    application = Application(
        external_id=opportunity.external_id,
        company=opportunity.company,
        position=opportunity.title,
        source=opportunity.source,
        job_url=str(opportunity.url),
        location=opportunity.location,
        description=opportunity.description,
        posted_at=opportunity.posted_at,
        recruiter=opportunity.recruiter,
        recruiter_linkedin_url=(
            str(opportunity.recruiter_linkedin_url) if opportunity.recruiter_linkedin_url else None
        ),
        professional_email=opportunity.professional_email,
        notes=opportunity.notes,
        score=score.score,
        score_reason=score.reason,
        relevance_category=score.relevance_category,
    )
    db.add(application)
    db.flush()
    db.add(
        ApplicationEvent(
            application_id=application.id,
            event_type="discovered",
            channel=opportunity.source,
            notes=f"Score {score.score}: {score.reason}",
        )
    )
    return application, True


def add_event(db: Session, application: Application, event: EventCreate) -> ApplicationEvent:
    model = ApplicationEvent(
        application_id=application.id,
        event_type=event.event_type,
        channel=event.channel,
        notes=event.notes,
    )
    db.add(model)
    return model
