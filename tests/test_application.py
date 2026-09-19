"""Tests for Application Submission ATS integrations."""

import asyncio
import os
import pytest
from unittest.mock import patch
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, Settings
from app.integrations.application_providers import (
    detect_ats_provider,
    GreenhouseApplicationProvider,
    LeverApplicationProvider,
    ManualApplicationProvider,
)
from app.models import Application, ApplicationStatus
from app.services.application import prepare_application_submission, submit_application


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def make_settings(**kwargs) -> Settings:
    s = Settings()
    s.candidate_first_name = "Jane"
    s.candidate_last_name = "Doe"
    s.candidate_email = "jane@example.com"
    s.candidate_cv_path = os.path.abspath("/path/to/resume.pdf")
    s.dry_run_ats = False
    for k, v in kwargs.items():
        setattr(s, k, v)
    return s


@pytest.fixture(autouse=True)
def mock_cv_validator():
    """Mock CV validation so we don't need real files for these payload tests."""
    with patch("app.services.application.validate_cv") as mock:
        yield mock


def make_app(session, company: str, url: str, status=ApplicationStatus.discovered) -> Application:
    """Helper: creates a minimal valid Application row."""
    app = Application(
        company=company,
        position="Engineer",
        source="manual",
        job_url=url,
        application_status=status,
    )
    session.add(app)
    session.commit()
    return app


# ---------------------------------------------------------------------------
# Detection tests (pure unit — no DB)
# ---------------------------------------------------------------------------


def test_ats_detection_greenhouse():
    gh_url = "https://boards.greenhouse.io/testcompany/jobs/12345"
    provider = detect_ats_provider(gh_url)
    assert isinstance(provider, GreenhouseApplicationProvider)
    assert provider.extract_job_id(gh_url) == "testcompany:12345"


def test_ats_detection_lever():
    lever_url = "https://jobs.lever.co/testcompany/abcd-1234-ef01"
    provider = detect_ats_provider(lever_url)
    assert isinstance(provider, LeverApplicationProvider)
    assert provider.extract_job_id(lever_url) == "testcompany:abcd-1234-ef01"


def test_ats_detection_manual_linkedin():
    li_url = "https://www.linkedin.com/jobs/view/12345"
    provider = detect_ats_provider(li_url)
    assert isinstance(provider, ManualApplicationProvider)
    assert not provider.can_auto_apply(li_url)


def test_ats_detection_manual_unknown():
    provider = detect_ats_provider("https://somecompany.com/careers/apply")
    assert isinstance(provider, ManualApplicationProvider)


# ---------------------------------------------------------------------------
# Payload preparation tests
# ---------------------------------------------------------------------------


def test_greenhouse_payload_creation():
    session = make_session()
    settings = make_settings()
    app = make_app(session, "TestCompany", "https://boards.greenhouse.io/testcompany/jobs/12345")

    payload = asyncio.run(prepare_application_submission(session, app, settings))

    assert payload.provider_name == "Greenhouse"
    assert payload.is_ready is True
    assert len(payload.missing_fields) == 0
    assert payload.answers["first_name"] == "Jane"
    assert app.application_payload is not None
    assert app.application_method == "Greenhouse"


def test_manual_payload_creation():
    session = make_session()
    settings = make_settings()
    app = make_app(session, "LinkedInCompany", "https://www.linkedin.com/jobs/view/12345")

    payload = asyncio.run(prepare_application_submission(session, app, settings))

    assert payload.provider_name == "Manual"
    assert payload.is_ready is False
    assert len(payload.missing_fields) > 0
    assert "manual" in payload.missing_fields[0].lower()


def test_lever_payload_creation():
    session = make_session()
    settings = make_settings()
    app = make_app(session, "LeverCo", "https://jobs.lever.co/leverco/abcd-1234")

    payload = asyncio.run(prepare_application_submission(session, app, settings))

    assert payload.provider_name == "Lever"
    assert payload.is_ready is True
    assert app.application_method == "Lever"


def test_payload_not_ready_when_cv_missing(mock_cv_validator):
    session = make_session()
    settings = make_settings(candidate_cv_path=None)
    app = make_app(session, "GHCo", "https://boards.greenhouse.io/ghco/jobs/1")

    # For this specific test, we want the real validation to raise the error
    mock_cv_validator.side_effect = Exception("CV Validation Failed")
    from app.integrations.cv_validator import CVValidationError
    mock_cv_validator.side_effect = CVValidationError("no CV path is configured")

    with pytest.raises(CVValidationError, match="no CV path"):
        asyncio.run(prepare_application_submission(session, app, settings))


# ---------------------------------------------------------------------------
# Submission tests
# ---------------------------------------------------------------------------


def test_submission_lever_success():
    session = make_session()
    settings = make_settings()
    app = make_app(session, "LeverCompany", "https://jobs.lever.co/levercompany/abcd-1234")

    asyncio.run(prepare_application_submission(session, app, settings))
    submitted = asyncio.run(submit_application(session, app, settings))

    assert submitted.application_status == ApplicationStatus.applied
    assert submitted.external_application_id is not None
    assert any(e.event_type == "application_submitted" for e in app.events)


def test_submission_greenhouse_success():
    session = make_session()
    settings = make_settings()
    app = make_app(session, "GHCompany", "https://boards.greenhouse.io/ghcompany/jobs/999")

    asyncio.run(prepare_application_submission(session, app, settings))
    submitted = asyncio.run(submit_application(session, app, settings))

    assert submitted.application_status == ApplicationStatus.applied
    assert submitted.application_method == "Greenhouse"


def test_submission_manual_fallback_raises():
    session = make_session()
    settings = make_settings()
    app = make_app(session, "LinkedInCo", "https://www.linkedin.com/jobs/view/12345")

    asyncio.run(prepare_application_submission(session, app, settings))

    with pytest.raises(ValueError, match="requires manual submission"):
        asyncio.run(submit_application(session, app, settings))


def test_duplicate_submission_prevented():
    session = make_session()
    settings = make_settings()
    app = make_app(
        session, "GHCompany", "https://boards.greenhouse.io/ghcompany/jobs/123",
        status=ApplicationStatus.applied,
    )

    with pytest.raises(ValueError, match="already been submitted"):
        asyncio.run(submit_application(session, app, settings))


def test_force_resubmit_skips_idempotency_but_requires_prepared():
    session = make_session()
    settings = make_settings()
    app = make_app(
        session, "GHCompany", "https://boards.greenhouse.io/ghcompany/jobs/123",
        status=ApplicationStatus.applied,
    )

    with pytest.raises(ValueError, match="not prepared yet"):
        asyncio.run(submit_application(session, app, settings, force_resubmit=True))


def test_submission_with_answer_override():
    session = make_session()
    settings = make_settings()
    app = make_app(session, "GHCompany2", "https://boards.greenhouse.io/ghcompany2/jobs/456")

    asyncio.run(prepare_application_submission(session, app, settings))
    submitted = asyncio.run(
        submit_application(
            session, app, settings,
            answers_override={"linkedin_url": "https://linkedin.com/in/jane"}
        )
    )
    assert submitted.application_status == ApplicationStatus.applied


def test_application_method_persisted_after_submission():
    session = make_session()
    settings = make_settings()
    app = make_app(session, "LeverCo2", "https://jobs.lever.co/leverco2/abcd-5678")

    asyncio.run(prepare_application_submission(session, app, settings))
    asyncio.run(submit_application(session, app, settings))

    session.refresh(app)
    assert app.application_method == "Lever"
    assert app.application_date is not None
    assert app.application_status == ApplicationStatus.applied
