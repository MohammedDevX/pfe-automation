from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Application, ApplicationEvent
from app.schemas import EventCreate, OpportunityIn
from app.scoring import ScoreResult, score_opportunity
from app.utils import canonicalize_url


def _find_existing_application(db: Session, opportunity: OpportunityIn) -> Application | None:
    raw_url = str(opportunity.url)
    canon_target = canonicalize_url(raw_url)
    normalized_company = opportunity.company.strip().lower()
    normalized_position = opportunity.title.strip().lower()
    normalized_location = (opportunity.location or "").strip().lower()

    # 1. Fast SQL query by exact job_url, canonical job_url, source+external_id, or company+position+location
    clauses = [Application.job_url == raw_url]
    if raw_url != canon_target:
        clauses.append(Application.job_url == canon_target)
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
    existing = db.scalar(select(Application).where(or_(*clauses)))
    if existing:
        return existing

    # 2. Canonical URL scan across stored records if exact clauses missed due to URL variations
    # (e.g. stored record has trailing slash or tracking query parameter)
    all_apps = db.scalars(select(Application)).all()
    for app in all_apps:
        if app.job_url and canonicalize_url(app.job_url) == canon_target:
            return app

    return None


def _update_application_fields(
    existing: Application, opportunity: OpportunityIn, score: ScoreResult
) -> Application:
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
    return existing


def upsert_opportunity(
    db: Session, opportunity: OpportunityIn, score: ScoreResult | None = None
) -> tuple[Application, bool]:
    score = score or score_opportunity(opportunity)
    existing = _find_existing_application(db, opportunity)

    if existing:
        _update_application_fields(existing, opportunity, score)
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

    try:
        with db.begin_nested():
            db.add(application)
            db.flush()
    except IntegrityError as exc:
        # Savepoint is automatically rolled back by begin_nested() context manager.
        # Check if the error is due to a duplicate opportunity already existing in DB
        conflicting = _find_existing_application(db, opportunity)
        if conflicting:
            _update_application_fields(conflicting, opportunity, score)
            return conflicting, False
        # If no conflicting application was found, re-raise unrelated IntegrityError
        raise exc

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
