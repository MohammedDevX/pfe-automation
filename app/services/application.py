"""Application submission service handling ATS integrations.

Flow:
  prepare_application_submission()  → validate CV, detect ATS, build payload
  submit_application()              → dry-run check, submit, record event

Dry-run mode (DRY_RUN_ATS=true, the default):
  - Payload is prepared and validated
  - CV is validated (exists, correct format, right size)
  - NO external API call is made
  - Raises DryRunError to inform the caller

Production mode (DRY_RUN_ATS=false):
  - Full submission is attempted
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import date

from sqlalchemy.orm import Session

from app.database import Settings
from app.integrations.application_providers import (
    ApplicationPayload,
    CandidateProfile,
    detect_ats_provider,
)
from app.integrations.cv_validator import CVValidationError, validate_cv
from app.models import Application, ApplicationStatus
from app.services.lifecycle import _add_event


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_candidate_profile(settings: Settings) -> CandidateProfile:
    """Load candidate profile from settings (single source of truth)."""
    return CandidateProfile(
        first_name=settings.candidate_first_name,
        last_name=settings.candidate_last_name,
        email=settings.candidate_email,
        phone=settings.candidate_phone,
        location=settings.candidate_location,
        education=settings.candidate_school,
        degree=settings.candidate_degree,
        specialization=settings.candidate_specialization,
        linkedin_url=settings.candidate_linkedin_url,
        github_url=settings.candidate_github_url,
        portfolio_url=settings.candidate_portfolio_url,
        cv_path=settings.candidate_cv_path,
        cover_letter=settings.candidate_cover_letter,
    )


# ---------------------------------------------------------------------------
# Prepare
# ---------------------------------------------------------------------------


async def prepare_application_submission(
    db: Session,
    application: Application,
    settings: Settings,
) -> ApplicationPayload:
    """Detect ATS, validate CV, build and persist payload.

    This does NOT submit anything externally.
    """
    provider = detect_ats_provider(application.job_url)
    candidate = _get_candidate_profile(settings)

    # CV validation — happens before any network call
    try:
        validate_cv(candidate.cv_path)
    except CVValidationError:
        # Re-raise as-is — message is already human-readable
        raise

    payload = await provider.prepare_application(application.job_url, candidate)

    # Persist payload so the human review UI can display / edit it
    application.application_method = provider.platform_name
    application.application_payload = {
        "provider_name": payload.provider_name,
        "is_ready": payload.is_ready,
        "missing_fields": payload.missing_fields,
        "questions": [asdict(q) for q in payload.questions],
        "answers": payload.answers,
        "candidate": payload.candidate.model_dump(),
    }

    db.commit()
    db.refresh(application)
    return payload


# ---------------------------------------------------------------------------
# Submit
# ---------------------------------------------------------------------------


async def submit_application(
    db: Session,
    application: Application,
    settings: Settings,
    answers_override: dict | None = None,
    force_resubmit: bool = False,
) -> Application:
    """Submit the application via the identified ATS provider.

    Dry-run mode (DRY_RUN_ATS=true, default):
        Validates everything, records a dry-run event, then raises
        ValueError so the caller (and UI) know no real submission happened.

    Production mode (DRY_RUN_ATS=false):
        Performs the real API call.
    """
    # 1. Idempotency guard
    if application.application_status == ApplicationStatus.applied and not force_resubmit:
        raise ValueError(
            "Application has already been submitted. Use force_resubmit=true to override."
        )

    if not application.application_payload:
        raise ValueError(
            "Application is not prepared yet. Call /prepare first."
        )

    provider = detect_ats_provider(application.job_url)

    # Rebuild candidate from stored payload (may have been edited in review UI)
    candidate_dict = application.application_payload["candidate"]
    candidate = CandidateProfile(**candidate_dict)

    # Re-validate CV before submission
    try:
        validate_cv(candidate.cv_path)
    except CVValidationError:
        raise

    # Re-prepare payload (applies any stored answers)
    payload = await provider.prepare_application(application.job_url, candidate)

    if answers_override:
        payload.answers.update(answers_override)

    # 2. Dry-run gate
    if settings.dry_run_ats:
        _add_event(
            db,
            application,
            event_type="application_dry_run",
            notes=(
                f"[DRY RUN] Provider: {provider.platform_name} | "
                f"Ready: {payload.is_ready} | "
                f"Missing: {payload.missing_fields or 'none'}"
            ),
        )
        db.commit()
        raise ValueError(
            f"[DRY RUN] Application was NOT submitted \u2014 dry_run_ats is enabled.\n"
            f"Provider: {provider.platform_name} | Ready: {payload.is_ready}\n"
            f"Set DRY_RUN_ATS=false in your .env to enable real submission."
        )

    # 3. Submit externally
    result = await provider.submit_application(application.job_url, payload)

    if result.requires_manual_action:
        raise ValueError(
            f"This platform ({provider.platform_name}) requires manual submission. "
            f"Please apply manually at {result.action_url or application.job_url}."
        )

    if not result.success:
        _add_event(
            db,
            application,
            event_type="application_failed",
            notes=f"Failed via {provider.platform_name}: {result.error_message}",
        )
        db.commit()
        db.refresh(application)
        raise RuntimeError(
            f"Application submission failed via {provider.platform_name}: {result.error_message}"
        )

    # 4. Success
    application.application_status = ApplicationStatus.applied
    application.application_date = date.today()
    application.external_application_id = result.external_id
    application.application_method = provider.platform_name

    _add_event(
        db,
        application,
        event_type="application_submitted",
        notes=(
            f"Submitted via {provider.platform_name}. "
            f"External ID: {result.external_id}"
        ),
    )

    db.commit()
    db.refresh(application)
    return application
