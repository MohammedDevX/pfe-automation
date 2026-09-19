"""Regression tests for workflow fixes.

Covers:
1. dry-run email does not become real "failed" (remains approved)
2. dry-run reason is explicit ("[DRY RUN] Email not sent — dry_run_email=True")
3. missing recipient blocks message generation
4. notion.so is never treated as company website
5. Notion-only application is manual-only
6. real external job URL remains usable
7. Mark Reviewed works
8. Skip works
9. existing Notion applications remain intact
"""
import asyncio
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, Settings
from app.integrations.application_providers import CandidateProfile, detect_ats_provider
from app.models import (
    Application,
    ApplicationStatus,
    Company,
    Contact,
    MessageChannel,
    MessageStatus,
    OutboundMessage,
    ProfessionalEmail,
    ReviewStatus,
)
from app.schemas import MessageGenerateRequest
from app.services.messaging import generate_message, send_message
from app.services.research import get_or_create_company, infer_company_website


def make_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


@pytest.fixture
def test_client():
    from fastapi.testclient import TestClient
    from app.main import app as fastapi_app
    from app.database import get_db

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(bind=engine)

    def override_get_db():
        try:
            db = TestingSessionLocal()
            yield db
        finally:
            db.close()

    fastapi_app.dependency_overrides[get_db] = override_get_db
    with TestClient(fastapi_app) as client:
        yield client
    fastapi_app.dependency_overrides.clear()


def test_dry_run_email_does_not_become_failed_and_reason_is_explicit():
    """Test 1 & 2: dry-run email keeps status approved and sets explicit reason."""
    session = make_session()
    app = Application(
        company="TestCorp",
        position="PFE Developer",
        source="manual",
        job_url="https://testcorp.ma/jobs/1",
        professional_email="recruiter@testcorp.ma",
    )
    session.add(app)
    session.commit()

    msg = OutboundMessage(
        application_id=app.id,
        channel=MessageChannel.email,
        subject="PFE Application",
        body="Bonjour, voici ma candidature.",
        status=MessageStatus.approved,
    )
    session.add(msg)
    session.commit()

    settings = Settings(dry_run_email=True)
    result = asyncio.run(send_message(session, msg, settings))

    # 1. Does NOT become 'failed'
    assert result.status == MessageStatus.approved
    # 2. Reason is explicit
    assert result.failure_reason == "[DRY RUN] Email not sent — dry_run_email=True"


def test_missing_recipient_blocks_generation():
    """Test 3: missing recipient blocks message generation with clear error."""
    session = make_session()
    app = Application(
        company="NoEmailCorp",
        position="PFE Developer",
        source="manual",
        job_url="https://noemail.ma/jobs/1",
        professional_email=None,  # No email!
    )
    session.add(app)
    session.commit()

    settings = Settings()
    req = MessageGenerateRequest(channel=MessageChannel.email)

    with pytest.raises(ValueError, match="No professional recipient email found. Run Research first."):
        asyncio.run(generate_message(session, app, req, settings))

    # Confirm no message draft was persisted
    drafts = list(session.scalars(select(OutboundMessage).where(OutboundMessage.application_id == app.id)))
    assert len(drafts) == 0


def test_notion_so_never_treated_as_company_website():
    """Test 4: notion.so URLs are never inferred or saved as company websites."""
    session = make_session()

    notion_url = "https://notion.so/3dce639ff8c7812d9881ef8aa7cccc3b"
    assert infer_company_website(notion_url) is None
    assert infer_company_website("https://www.notion.so/my-page") is None

    app = Application(
        company="NotionCo",
        position="PFE Angular",
        source="notion_crm",
        job_url=notion_url,
    )
    session.add(app)
    session.commit()

    company = get_or_create_company(session, app, website=notion_url)
    assert company.website is None or "notion.so" not in company.website.lower()


def test_notion_only_application_is_manual_only():
    """Test 5: Notion-only application resolves to Manual provider with clear note."""
    notion_url = "https://notion.so/3dce639ff8c7812d9881ef8aa7cccc3b"
    provider = detect_ats_provider(notion_url)
    assert provider.platform_name == "Manual"

    cand = CandidateProfile(
        first_name="Alice",
        last_name="Martin",
        email="alice@example.com",
        phone="0600000000",
        cv_path="tests/dummy_cv.pdf",
    )
    payload = asyncio.run(provider.prepare_application(notion_url, cand))
    assert payload.provider_name == "Manual"
    assert payload.is_ready is False
    assert "No external job listing URL available." in payload.missing_fields

    result = asyncio.run(provider.submit_application(notion_url, payload))
    assert result.success is False
    assert result.requires_manual_action is True
    assert result.action_url is None
    assert result.error_message == "No external job listing URL available."


def test_real_external_job_url_remains_usable():
    """Test 6: Real ATS URLs (Greenhouse, Lever) remain auto-detectable and usable."""
    gh_url = "https://boards.greenhouse.io/acme/jobs/12345"
    provider = detect_ats_provider(gh_url)
    assert provider.platform_name == "Greenhouse"
    assert provider.can_auto_apply(gh_url) is True

    lever_url = "https://jobs.lever.co/acme/abcd-1234"
    provider_lever = detect_ats_provider(lever_url)
    assert provider_lever.platform_name == "Lever"
    assert provider_lever.can_auto_apply(lever_url) is True


def test_mark_reviewed_works(test_client):
    """Test 7: Mark Reviewed endpoint updates review_status to approved."""
    client = test_client
    ingest = client.post(
        "/opportunities/ingest",
        json={
            "source": "manual",
            "title": "PFE Dev",
            "company": "ReviewCorp",
            "url": "https://reviewcorp.ma/jobs/1",
        },
    )
    app_id = ingest.json()["id"]
    assert ingest.json()["review_status"] == "pending"

    r = client.post(f"/applications/{app_id}/review", json={"review_status": "approved"})
    assert r.status_code == 200
    assert r.json()["review_status"] == "approved"


def test_skip_works(test_client):
    """Test 8: Skip endpoint updates review_status to rejected."""
    client = test_client
    ingest = client.post(
        "/opportunities/ingest",
        json={
            "source": "manual",
            "title": "PFE Dev",
            "company": "SkipCorp",
            "url": "https://skipcorp.ma/jobs/1",
        },
    )
    app_id = ingest.json()["id"]

    r = client.post(f"/applications/{app_id}/review", json={"review_status": "rejected"})
    assert r.status_code == 200
    assert r.json()["review_status"] == "rejected"


def test_existing_notion_applications_remain_intact():
    """Test 9: Existing Notion application fields (notion_page_id, etc.) remain intact."""
    session = make_session()
    app = Application(
        company="E-AMBITION",
        position="Stage Développeur Angular",
        source="notion_crm",
        job_url="https://notion.so/3dce639ff8c7812d9881ef8aa7cccc3b",
        notion_page_id="3dce639f-f8c7-812d-9881-ef8aa7cccc3b",
        score=68,
        relevance_category="EXPLICIT_INTERNSHIP",
        review_status=ReviewStatus.pending,
    )
    session.add(app)
    session.commit()

    fetched = session.get(Application, app.id)
    assert fetched.notion_page_id == "3dce639f-f8c7-812d-9881-ef8aa7cccc3b"
    assert fetched.company == "E-AMBITION"
    assert fetched.score == 68
    assert fetched.review_status == ReviewStatus.pending
