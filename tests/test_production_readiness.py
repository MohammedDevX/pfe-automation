"""Production-readiness tests: CV validation, dry-run modes, candidate profile,
configuration validation, and observability.

These tests exercise the safety gates that protect against accidentally sending
real emails or submitting real applications during development.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, Settings
from app.integrations.cv_validator import (
    CVValidationError,
    MAX_CV_SIZE_BYTES,
    SUPPORTED_EXTENSIONS,
    validate_cv,
)
from app.integrations.email_senders import (
    DryRunEmailSender,
    EmailSendError,
    NullEmailSender,
    build_email_sender,
)
from app.integrations.application_providers import detect_ats_provider
from app.models import Application, ApplicationStatus
from app.services.application import prepare_application_submission, submit_application


# ---------------------------------------------------------------------------
# Fixtures
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
    s.candidate_phone = "+212 600 000 000"
    # Both dry-run flags ON by default in tests
    s.dry_run_email = True
    s.dry_run_ats = True
    for k, v in kwargs.items():
        setattr(s, k, v)
    return s


def make_app(session, url: str = "https://boards.greenhouse.io/co/jobs/1") -> Application:
    app = Application(
        company="TestCo",
        position="Engineer",
        source="manual",
        job_url=url,
    )
    session.add(app)
    session.commit()
    return app


# ===========================================================================
# CV VALIDATION
# ===========================================================================


def test_cv_none_raises_clear_message():
    with pytest.raises(CVValidationError, match="no CV path is configured"):
        validate_cv(None)


def test_cv_empty_string_raises():
    with pytest.raises(CVValidationError, match="no CV path is configured"):
        validate_cv("")


def test_cv_relative_path_raises():
    with pytest.raises(CVValidationError, match="must be absolute"):
        validate_cv("relative/path/cv.pdf")


def test_cv_missing_file_raises():
    with pytest.raises(CVValidationError, match="CV file not found"):
        validate_cv(os.path.abspath("/nonexistent/path/cv.pdf"))


def test_cv_unsupported_extension_raises():
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
        f.write(b"not a cv")
        tmp_path = f.name
    try:
        with pytest.raises(CVValidationError, match="unsupported CV file format"):
            validate_cv(tmp_path)
    finally:
        os.unlink(tmp_path)


def test_cv_empty_file_raises():
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        tmp_path = f.name  # write nothing
    try:
        with pytest.raises(CVValidationError, match="empty"):
            validate_cv(tmp_path)
    finally:
        os.unlink(tmp_path)


def test_cv_valid_pdf_passes():
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(b"%PDF-1.4 fake pdf content")
        tmp_path = f.name
    try:
        result = validate_cv(tmp_path)
        assert result == Path(tmp_path)
    finally:
        os.unlink(tmp_path)


def test_cv_valid_docx_passes():
    with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as f:
        f.write(b"PK fake docx content")
        tmp_path = f.name
    try:
        result = validate_cv(tmp_path)
        assert isinstance(result, Path)
    finally:
        os.unlink(tmp_path)


def test_cv_directory_raises():
    with tempfile.TemporaryDirectory() as tmp_dir:
        # Rename a dir to have .pdf suffix by creating a nested path
        # We can't easily do this cross-platform, so test with the actual dir
        # by using a path that exists but is a directory
        # Instead, create a temp file, validate it passes, then test the dir case
        # by just calling with the dir path:
        # Actually the file must have supported extension; let's just test the logic
        # that a directory path is rejected:
        with pytest.raises(CVValidationError):
            validate_cv(tmp_dir)  # directory, not a file (no valid extension either)


def test_cv_supported_extensions_set():
    assert ".pdf" in SUPPORTED_EXTENSIONS
    assert ".docx" in SUPPORTED_EXTENSIONS
    assert ".txt" not in SUPPORTED_EXTENSIONS


# ===========================================================================
# DRY-RUN EMAIL
# ===========================================================================


def test_build_email_sender_returns_dry_run_when_flag_set():
    settings = make_settings(dry_run_email=True)
    sender = build_email_sender(settings)
    assert isinstance(sender, DryRunEmailSender)


def test_build_email_sender_returns_null_when_no_credentials():
    settings = make_settings(dry_run_email=False)
    settings.smtp_user = None
    settings.smtp_password = None
    sender = build_email_sender(settings)
    assert isinstance(sender, NullEmailSender)


def test_dry_run_email_sender_blocks_delivery():
    sender = DryRunEmailSender()
    with pytest.raises(EmailSendError, match="DRY RUN"):
        asyncio.run(sender.send(
            to="recruiter@company.com",
            subject="PFE Application",
            body="Hello...",
            from_addr="jane@example.com",
        ))


def test_dry_run_email_sender_captures_content():
    sender = DryRunEmailSender()
    try:
        asyncio.run(sender.send(
            to="recruiter@company.com",
            subject="PFE Application",
            body="Hello, I am applying...",
            from_addr="jane@example.com",
        ))
    except EmailSendError:
        pass
    assert len(sender.captured) == 1
    assert sender.captured[0]["to"] == "recruiter@company.com"
    assert sender.captured[0]["subject"] == "PFE Application"


def test_dry_run_error_message_contains_instructions():
    sender = DryRunEmailSender()
    try:
        asyncio.run(sender.send(
            to="x@y.com", subject="S", body="B", from_addr="f@f.com"
        ))
    except EmailSendError as exc:
        assert "DRY_RUN_EMAIL=false" in str(exc)


# ===========================================================================
# DRY-RUN ATS
# ===========================================================================


def test_prepare_fails_cleanly_without_cv():
    session = make_session()
    settings = make_settings()  # cv_path is None by default
    app = make_app(session)

    with pytest.raises(CVValidationError, match="no CV path is configured"):
        asyncio.run(prepare_application_submission(session, app, settings))


def test_prepare_succeeds_with_valid_cv(tmp_path):
    cv = tmp_path / "cv.pdf"
    cv.write_bytes(b"%PDF-1.4 content")

    session = make_session()
    settings = make_settings(candidate_cv_path=str(cv))
    app = make_app(session)

    payload = asyncio.run(prepare_application_submission(session, app, settings))
    assert payload.provider_name == "Greenhouse"
    assert payload.is_ready is True


def test_submit_blocked_by_dry_run_ats(tmp_path):
    cv = tmp_path / "cv.pdf"
    cv.write_bytes(b"%PDF-1.4 content")

    session = make_session()
    settings = make_settings(candidate_cv_path=str(cv), dry_run_ats=True)
    app = make_app(session)

    asyncio.run(prepare_application_submission(session, app, settings))

    with pytest.raises(ValueError, match="DRY RUN"):
        asyncio.run(submit_application(session, app, settings))


def test_submit_dry_run_records_event(tmp_path):
    cv = tmp_path / "cv.pdf"
    cv.write_bytes(b"%PDF-1.4 content")

    session = make_session()
    settings = make_settings(candidate_cv_path=str(cv), dry_run_ats=True)
    app = make_app(session)

    asyncio.run(prepare_application_submission(session, app, settings))

    try:
        asyncio.run(submit_application(session, app, settings))
    except ValueError:
        pass

    session.refresh(app)
    assert any(e.event_type == "application_dry_run" for e in app.events)


def test_submit_proceeds_when_dry_run_disabled(tmp_path):
    cv = tmp_path / "cv.pdf"
    cv.write_bytes(b"%PDF-1.4 content")

    session = make_session()
    settings = make_settings(candidate_cv_path=str(cv), dry_run_ats=False)
    app = make_app(session)

    asyncio.run(prepare_application_submission(session, app, settings))
    submitted = asyncio.run(submit_application(session, app, settings))

    assert submitted.application_status == ApplicationStatus.applied


def test_submit_without_prepare_raises_clear_message():
    session = make_session()
    settings = make_settings(dry_run_ats=False)
    app = make_app(session)

    with pytest.raises(ValueError, match="not prepared yet"):
        asyncio.run(submit_application(session, app, settings))


# ===========================================================================
# CANDIDATE PROFILE / SETTINGS
# ===========================================================================


def test_settings_candidate_name_property():
    s = Settings()
    s.candidate_first_name = "Jane"
    s.candidate_last_name = "Doe"
    assert s.candidate_name == "Jane Doe"


def test_settings_candidate_education_property():
    s = Settings()
    s.candidate_school = "ENSIAS"
    assert s.candidate_education == "ENSIAS"


def test_settings_defaults_have_dry_run_enabled():
    s = Settings()
    assert s.dry_run_email is True
    assert s.dry_run_ats is True


def test_settings_all_candidate_fields_present():
    """Ensure no candidate field is accidentally removed."""
    s = Settings()
    required_fields = [
        "candidate_first_name", "candidate_last_name",
        "candidate_email", "candidate_phone",
        "candidate_location", "candidate_school",
        "candidate_degree", "candidate_specialization",
        "candidate_skills", "candidate_linkedin_url",
        "candidate_github_url", "candidate_cv_path",
        "candidate_cover_letter", "candidate_cv_summary",
        "candidate_cv_summary_fr", "candidate_cv_summary_en",
    ]
    for field in required_fields:
        assert hasattr(s, field), f"Missing setting: {field}"


# ===========================================================================
# URL NORMALIZATION / ATS DETECTION EDGE CASES
# ===========================================================================


def test_greenhouse_url_with_trailing_slash():
    url = "https://boards.greenhouse.io/acme/jobs/12345/"
    provider = detect_ats_provider(url)
    from app.integrations.application_providers import GreenhouseApplicationProvider
    assert isinstance(provider, GreenhouseApplicationProvider)


def test_lever_url_with_query_params():
    url = "https://jobs.lever.co/acme/abcd-1234-ef01?lever-origin=applied"
    provider = detect_ats_provider(url)
    from app.integrations.application_providers import LeverApplicationProvider
    assert isinstance(provider, LeverApplicationProvider)


def test_empty_url_returns_manual():
    from app.integrations.application_providers import ManualApplicationProvider
    assert isinstance(detect_ats_provider(""), ManualApplicationProvider)
    assert isinstance(detect_ats_provider(None), ManualApplicationProvider)


def test_indeed_url_returns_manual():
    from app.integrations.application_providers import ManualApplicationProvider
    url = "https://www.indeed.com/viewjob?jk=abc123"
    assert isinstance(detect_ats_provider(url), ManualApplicationProvider)
