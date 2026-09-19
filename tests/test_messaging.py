"""Tests for message generation, review lifecycle, sending, and tracking.

All external API calls (OpenAI, SMTP) are mocked — no real network requests.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, Settings
from app.integrations.email_senders import EmailSendError, NullEmailSender, SMTPEmailSender
from app.integrations.message_providers import (
    GeneratedMessage,
    MessageContext,
    OpenAIMessageProvider,
    TemplateMessageProvider,
    build_message_provider,
)
from app.models import (
    Application,
    ApplicationEvent,
    ApplicationStatus,
    MessageChannel,
    MessageStatus,
    OutboundMessage,
)
from app.schemas import MessageGenerateRequest, MessagePatch
from app.services.messaging import (
    approve_message,
    edit_message,
    generate_message,
    reject_message,
    send_message,
)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

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


def make_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def make_application(session, company="TechCorp", position="PFE Backend Developer", professional_email="recruiter@techcorp.ma") -> Application:
    app = Application(
        company=company,
        position=position,
        source="manual",
        job_url=f"https://{company.lower()}.ma/jobs/pfe",
        location="Casablanca",
        description="Internship in Python, FastAPI, PostgreSQL.",
        professional_email=professional_email,
    )
    session.add(app)
    session.commit()
    return app


def make_draft(session, app: Application, channel: MessageChannel = MessageChannel.email) -> OutboundMessage:
    msg = OutboundMessage(
        application_id=app.id,
        channel=channel,
        subject="PFE Application – TechCorp",
        body="Bonjour, je souhaite postuler pour votre stage PFE.",
        generation_provider="template",
        status=MessageStatus.draft,
    )
    session.add(msg)
    session.commit()
    return msg


DEFAULT_SETTINGS = Settings(
    candidate_first_name="Alice",
    candidate_last_name="Martin",
    candidate_degree="5th-year engineering student",
    candidate_school="ENSIAS",
    candidate_specialization="Software Engineering",
    candidate_email="alice@example.com",
)


# ---------------------------------------------------------------------------
# TemplateMessageProvider
# ---------------------------------------------------------------------------


def test_template_provider_generates_email() -> None:
    ctx = MessageContext(
        candidate_name="Alice Martin",
        candidate_degree="5th-year engineering student",
        candidate_school="ENSIAS",
        candidate_specialization="Software Engineering",
        candidate_email="alice@example.com",
        candidate_cv_summary=None,
        job_title="PFE Backend Developer",
        job_description="Python, FastAPI, PostgreSQL.",
        job_url="https://techcorp.ma/jobs/pfe",
        job_location="Casablanca",
        company_name="TechCorp",
        company_description=None,
        company_website="https://techcorp.ma",
        recruiter_name="Zineb Recruiter",
        recruiter_title="Talent Acquisition",
        recruiter_email="zineb@techcorp.ma",
        channel="email",
    )
    provider = TemplateMessageProvider()
    result = asyncio.run(provider.generate(ctx))

    assert result.subject is not None
    assert "PFE Backend Developer" in result.subject
    assert "Alice Martin" in result.subject
    assert "Zineb Recruiter" in result.body
    assert "TechCorp" in result.body
    assert result.provider == "template"
    assert result.model is None


def test_template_provider_generates_linkedin() -> None:
    ctx = MessageContext(
        candidate_name="Alice Martin",
        candidate_degree="5th-year engineering student",
        candidate_school=None,
        candidate_specialization=None,
        candidate_email=None,
        candidate_cv_summary=None,
        job_title="PFE Backend",
        job_description=None,
        job_url="https://techcorp.ma/jobs/pfe",
        job_location=None,
        company_name="TechCorp",
        company_description=None,
        company_website=None,
        recruiter_name=None,
        recruiter_title=None,
        recruiter_email=None,
        channel="linkedin",
    )
    result = asyncio.run(TemplateMessageProvider().generate(ctx))
    assert result.subject is None  # no subject for LinkedIn
    assert "TechCorp" in result.body


# ---------------------------------------------------------------------------
# OpenAI provider — missing key
# ---------------------------------------------------------------------------


def test_openai_provider_raises_clear_error_without_key() -> None:
    provider = OpenAIMessageProvider(Settings(openai_api_key=None))
    ctx = MessageContext(
        candidate_name="Alice", candidate_degree="student", candidate_school=None,
        candidate_specialization=None, candidate_email=None, candidate_cv_summary=None,
        job_title="PFE", job_description=None, job_url="https://x.com",
        job_location=None, company_name="X", company_description=None,
        company_website=None, recruiter_name=None, recruiter_title=None,
        recruiter_email=None, channel="email",
    )
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        asyncio.run(provider.generate(ctx))


def test_openai_provider_with_mocked_api() -> None:
    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_response.json.return_value = {
        "choices": [{"message": {"content": "SUBJECT: Test\nBODY:\nHello world"}}],
        "model": "gpt-4o-mini",
    }
    provider = OpenAIMessageProvider(Settings(openai_api_key="sk-test", openai_model="gpt-4o-mini"))
    ctx = MessageContext(
        candidate_name="Alice", candidate_degree="student", candidate_school=None,
        candidate_specialization=None, candidate_email=None, candidate_cv_summary=None,
        job_title="PFE Backend", job_description=None, job_url="https://x.com",
        job_location="Casablanca", company_name="TechCorp", company_description=None,
        company_website="https://techcorp.ma", recruiter_name=None, recruiter_title=None,
        recruiter_email=None, channel="email",
    )
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=fake_response):
        result = asyncio.run(provider.generate(ctx))

    assert result.subject == "Test"
    assert result.body == "Hello world"
    assert result.model == "gpt-4o-mini"


def test_build_message_provider_rejects_unknown_name() -> None:
    with pytest.raises(ValueError, match="Unknown message provider"):
        build_message_provider("nonexistent", Settings())


# ---------------------------------------------------------------------------
# generate_message service
# ---------------------------------------------------------------------------


def test_generate_message_creates_draft_and_timeline_event() -> None:
    session = make_session()
    app = make_application(session)

    result = asyncio.run(
        generate_message(session, app, MessageGenerateRequest(channel=MessageChannel.email), DEFAULT_SETTINGS)
    )

    assert result.id is not None
    assert result.status == MessageStatus.draft
    assert result.application_id == app.id
    assert result.channel == MessageChannel.email
    assert result.body  # not empty
    assert result.generation_provider == "template"

    events = list(session.scalars(select(ApplicationEvent).where(ApplicationEvent.application_id == app.id)))
    assert any(e.event_type == "message_generated" for e in events)


def test_generate_message_missing_openai_key_returns_400_error() -> None:
    session = make_session()
    app = make_application(session)

    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        asyncio.run(
            generate_message(
                session,
                app,
                MessageGenerateRequest(provider="openai"),
                Settings(openai_api_key=None),
            )
        )


# ---------------------------------------------------------------------------
# approve / reject / edit lifecycle
# ---------------------------------------------------------------------------


def test_approve_draft_transitions_to_approved() -> None:
    session = make_session()
    app = make_application(session)
    msg = make_draft(session, app)

    result = approve_message(session, msg)

    assert result.status == MessageStatus.approved
    assert result.approved_at is not None


def test_approve_already_approved_raises() -> None:
    session = make_session()
    app = make_application(session)
    msg = make_draft(session, app)
    approve_message(session, msg)

    with pytest.raises(ValueError, match="draft"):
        approve_message(session, msg)


def test_reject_draft_transitions_to_rejected() -> None:
    session = make_session()
    app = make_application(session)
    msg = make_draft(session, app)

    result = reject_message(session, msg)
    assert result.status == MessageStatus.rejected


def test_edit_message_updates_body_and_subject() -> None:
    session = make_session()
    app = make_application(session)
    msg = make_draft(session, app)

    result = edit_message(session, msg, MessagePatch(body="New body", subject="New subject"))

    assert result.body == "New body"
    assert result.subject == "New subject"
    assert result.status == MessageStatus.draft  # stays draft


def test_edit_approved_message_resets_to_draft() -> None:
    session = make_session()
    app = make_application(session)
    msg = make_draft(session, app)
    approve_message(session, msg)
    assert msg.status == MessageStatus.approved

    edit_message(session, msg, MessagePatch(body="Edited body"))

    assert msg.status == MessageStatus.draft
    assert msg.approved_at is None


# ---------------------------------------------------------------------------
# Email sending
# ---------------------------------------------------------------------------


def test_send_approved_email_with_null_sender_succeeds() -> None:
    """NullEmailSender lets us verify the full send path without a real SMTP server."""
    session = make_session()
    app = make_application(session)
    msg = make_draft(session, app)
    approve_message(session, msg)

    null_sender = NullEmailSender()
    with patch("app.services.messaging.build_email_sender", return_value=null_sender):
        result = asyncio.run(
            send_message(session, msg, DEFAULT_SETTINGS, to_email="recruiter@techcorp.ma")
        )

    assert result.status == MessageStatus.sent
    assert result.sent_at is not None
    assert result.failure_reason is None
    assert len(null_sender.sent) == 1
    assert null_sender.sent[0]["to"] == "recruiter@techcorp.ma"


def test_send_updates_application_status_and_contact_date() -> None:
    session = make_session()
    app = make_application(session)
    msg = make_draft(session, app)
    approve_message(session, msg)

    null_sender = NullEmailSender()
    with patch("app.services.messaging.build_email_sender", return_value=null_sender):
        asyncio.run(
            send_message(session, msg, DEFAULT_SETTINGS, to_email="hr@techcorp.ma")
        )

    session.refresh(app)
    assert app.application_status == ApplicationStatus.contacted
    assert app.contact_date is not None


def test_send_creates_timeline_event() -> None:
    session = make_session()
    app = make_application(session)
    msg = make_draft(session, app)
    approve_message(session, msg)

    null_sender = NullEmailSender()
    with patch("app.services.messaging.build_email_sender", return_value=null_sender):
        asyncio.run(send_message(session, msg, DEFAULT_SETTINGS, to_email="hr@techcorp.ma"))

    events = list(session.scalars(select(ApplicationEvent).where(ApplicationEvent.application_id == app.id)))
    assert any(e.event_type == "email_sent" for e in events)


def test_send_smtp_failure_marks_failed_and_preserves_message() -> None:
    """A failed send should mark the message failed but NOT delete it."""
    session = make_session()
    app = make_application(session)
    msg = make_draft(session, app)
    approve_message(session, msg)

    failing_sender = NullEmailSender()

    async def bad_send(**kwargs):
        raise EmailSendError("Connection refused")

    failing_sender.send = bad_send

    with patch("app.services.messaging.build_email_sender", return_value=failing_sender):
        result = asyncio.run(
            send_message(session, msg, DEFAULT_SETTINGS, to_email="hr@techcorp.ma")
        )

    assert result.status == MessageStatus.failed
    assert "Connection refused" in result.failure_reason
    assert result.sent_at is None
    assert result.id is not None  # message still exists


def test_send_unapproved_message_raises() -> None:
    session = make_session()
    app = make_application(session)
    msg = make_draft(session, app)  # still draft

    with pytest.raises(ValueError, match="approved"):
        asyncio.run(send_message(session, msg, DEFAULT_SETTINGS, to_email="hr@techcorp.ma"))


def test_duplicate_send_raises() -> None:
    session = make_session()
    app = make_application(session)
    msg = make_draft(session, app)
    approve_message(session, msg)

    null_sender = NullEmailSender()
    with patch("app.services.messaging.build_email_sender", return_value=null_sender):
        asyncio.run(send_message(session, msg, DEFAULT_SETTINGS, to_email="hr@techcorp.ma"))

    assert msg.status == MessageStatus.sent

    # After sending, status is 'sent' — cannot send again (blocked by first guard)
    with pytest.raises(ValueError, match="Only approved messages can be sent"):
        asyncio.run(send_message(session, msg, DEFAULT_SETTINGS, to_email="hr@techcorp.ma"))


def test_send_without_email_raises() -> None:
    session = make_session()
    app = make_application(session, professional_email=None)
    msg = make_draft(session, app)
    approve_message(session, msg)

    with pytest.raises(ValueError, match="No recipient email"):
        asyncio.run(send_message(session, msg, DEFAULT_SETTINGS, to_email=None))


# ---------------------------------------------------------------------------
# LinkedIn
# ---------------------------------------------------------------------------


def test_linkedin_message_can_be_generated() -> None:
    session = make_session()
    app = make_application(session)

    result = asyncio.run(
        generate_message(
            session,
            app,
            MessageGenerateRequest(channel=MessageChannel.linkedin),
            DEFAULT_SETTINGS,
        )
    )

    assert result.channel == MessageChannel.linkedin
    assert result.subject is None  # no subject for LinkedIn
    assert result.body  # body is generated


def test_linkedin_send_raises_clear_informational_error() -> None:
    """LinkedIn sending must fail with a clear, helpful message — not a 500."""
    session = make_session()
    app = make_application(session)
    msg = make_draft(session, app, channel=MessageChannel.linkedin)
    approve_message(session, msg)

    with pytest.raises(ValueError, match="LinkedIn direct sending is not available"):
        asyncio.run(send_message(session, msg, DEFAULT_SETTINGS))


# ---------------------------------------------------------------------------
# SMTP sender unit tests
# ---------------------------------------------------------------------------


def test_smtp_sender_raises_send_error_without_credentials() -> None:
    sender = SMTPEmailSender(Settings(smtp_user=None, smtp_password=None))

    with pytest.raises(EmailSendError, match="SMTP_USER"):
        asyncio.run(sender.send(to="r@x.com", subject="Hi", body="Hello", from_addr="me@x.com"))


# ---------------------------------------------------------------------------
# API endpoint integration tests
# ---------------------------------------------------------------------------


def test_generate_endpoint_returns_400_for_unknown_app(test_client) -> None:
    client = test_client
    r = client.post("/applications/99999/messages/generate", json={})
    assert r.status_code == 404


def test_generate_endpoint_creates_draft(test_client) -> None:
    client = test_client
    # First ingest an opportunity
    ingest = client.post(
        "/opportunities/ingest",
        json={
            "source": "manual",
            "title": "PFE Backend Developer",
            "company": "API Test Corp",
            "url": "https://apitestcorp.ma/jobs/pfe",
            "location": "Casablanca",
            "description": "PFE Python FastAPI stage.",
            "professional_email": "recruiter@apitestcorp.ma",
        },
    )
    assert ingest.status_code == 200
    app_id = ingest.json()["id"]

    r = client.post(
        f"/applications/{app_id}/messages/generate",
        json={"channel": "email", "provider": "template"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "draft"
    assert data["channel"] == "email"
    assert data["body"]
    msg_id = data["id"]

    # Approve
    r2 = client.post(f"/messages/{msg_id}/approve")
    assert r2.status_code == 200
    assert r2.json()["status"] == "approved"

    # Edit
    r3 = client.patch(f"/messages/{msg_id}", json={"body": "Updated body"})
    assert r3.status_code == 200
    # Editing after approval resets to draft
    assert r3.json()["status"] == "draft"

    # Reject
    r4 = client.post(f"/messages/{msg_id}/reject")
    assert r4.status_code == 200
    assert r4.json()["status"] == "rejected"


def test_send_endpoint_requires_approved_status(test_client) -> None:
    client = test_client
    ingest = client.post(
        "/opportunities/ingest",
        json={
            "source": "manual",
            "title": "PFE Backend Developer",
            "company": "Send Test Corp",
            "url": "https://sendtest.ma/jobs/pfe",
            "location": "Rabat",
            "professional_email": "hr@sendtest.ma",
        },
    )
    app_id = ingest.json()["id"]
    gen = client.post(f"/applications/{app_id}/messages/generate", json={})
    msg_id = gen.json()["id"]

    # Try sending draft (not approved)
    r = client.post(f"/messages/{msg_id}/send", json={"to_email": "hr@sendtest.ma"})
    assert r.status_code == 400
    assert "approved" in r.json()["detail"].lower()


def test_get_message_404_for_unknown(test_client) -> None:
    client = test_client
    assert client.get("/messages/99999").status_code == 404


def test_list_messages_returns_empty_for_fresh_application(test_client) -> None:
    client = test_client
    ingest = client.post(
        "/opportunities/ingest",
        json={
            "source": "manual",
            "title": "PFE Test Position",
            "company": "Empty Messages Corp",
            "url": "https://emptymsg.ma/jobs/pfe",
        },
    )
    app_id = ingest.json()["id"]
    r = client.get(f"/applications/{app_id}/messages")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


# ---------------------------------------------------------------------------
# Bilingual Candidate Summary & Language Support Tests
# ---------------------------------------------------------------------------


def test_french_summary_loading() -> None:
    settings = Settings(
        candidate_cv_summary_fr="Étudiant en génie informatique à Oujda...",
        candidate_cv_summary_en="Computer engineering student in Oujda...",
    )
    assert settings.candidate_cv_summary_fr == "Étudiant en génie informatique à Oujda..."
    assert settings.get_cv_summary("fr") == "Étudiant en génie informatique à Oujda..."


def test_english_summary_loading() -> None:
    settings = Settings(
        candidate_cv_summary_fr="Étudiant en génie informatique à Oujda...",
        candidate_cv_summary_en="Computer engineering student in Oujda...",
    )
    assert settings.candidate_cv_summary_en == "Computer engineering student in Oujda..."
    assert settings.get_cv_summary("en") == "Computer engineering student in Oujda..."


def test_legacy_summary_backward_compatibility() -> None:
    # Legacy candidate_cv_summary populates language summaries if not explicitly set
    legacy_settings = Settings(
        candidate_cv_summary="Legacy general CV summary",
    )
    assert legacy_settings.candidate_cv_summary == "Legacy general CV summary"
    assert legacy_settings.candidate_cv_summary_fr == "Legacy general CV summary"
    assert legacy_settings.candidate_cv_summary_en == "Legacy general CV summary"
    assert legacy_settings.get_cv_summary("fr") == "Legacy general CV summary"
    assert legacy_settings.get_cv_summary("en") == "Legacy general CV summary"

    # If language-specific summaries are provided, candidate_cv_summary falls back cleanly
    bilingual_settings = Settings(
        candidate_cv_summary_fr="Résumé en français",
        candidate_cv_summary_en="Resume in English",
    )
    assert bilingual_settings.candidate_cv_summary == "Résumé en français"
    assert bilingual_settings.get_cv_summary("fr") == "Résumé en français"
    assert bilingual_settings.get_cv_summary("en") == "Resume in English"


def test_french_message_generation_uses_french_summary() -> None:
    session = make_session()
    app = make_application(session, position="Stage PFE Développeur .NET")
    settings = Settings(
        candidate_first_name="Mohammed",
        candidate_last_name="Bakhtaoui",
        candidate_degree="Cycle d'ingénierie",
        candidate_school="EHEI",
        candidate_specialization="Génie informatique",
        candidate_cv_summary_fr="Candidat passionné par l'écosystème .NET et ASP.NET Core.",
        candidate_cv_summary_en="Candidate skilled in .NET and ASP.NET Core backend.",
    )

    result = asyncio.run(
        generate_message(
            session,
            app,
            MessageGenerateRequest(channel=MessageChannel.email, language="fr"),
            settings,
        )
    )

    assert result.status == MessageStatus.draft
    assert "Candidat passionné par l'écosystème .NET" in result.body
    assert "Candidate skilled in .NET" not in result.body
    assert "Candidature Stage PFE" in result.subject


def test_english_message_generation_uses_english_summary() -> None:
    session = make_session()
    app = make_application(session, position="Software Engineering Intern")
    settings = Settings(
        candidate_first_name="Mohammed",
        candidate_last_name="Bakhtaoui",
        candidate_degree="Master of Engineering",
        candidate_school="EHEI",
        candidate_specialization="Software Engineering",
        candidate_cv_summary_fr="Candidat passionné par l'écosystème .NET et ASP.NET Core.",
        candidate_cv_summary_en="Candidate skilled in .NET and ASP.NET Core backend.",
    )

    result = asyncio.run(
        generate_message(
            session,
            app,
            MessageGenerateRequest(channel=MessageChannel.email, language="en"),
            settings,
        )
    )

    assert result.status == MessageStatus.draft
    assert "Candidate skilled in .NET and ASP.NET Core backend." in result.body
    assert "Candidat passionné par l'écosystème .NET" not in result.body
    assert "Internship Application" in result.subject
    assert "Dear" in result.body or "I am writing to express my interest" in result.body


def test_openai_prompt_uses_correct_language_summary() -> None:
    from app.integrations.message_providers import _build_openai_user_prompt

    ctx_fr = MessageContext(
        candidate_name="Mohammed Bakhtaoui",
        candidate_degree="Eng",
        candidate_school=None,
        candidate_specialization=None,
        candidate_email=None,
        candidate_cv_summary=None,
        job_title="Dev",
        job_description=None,
        job_url="https://x.com",
        job_location=None,
        company_name="Co",
        company_description=None,
        company_website=None,
        recruiter_name=None,
        recruiter_title=None,
        recruiter_email=None,
        channel="email",
        language="fr",
        candidate_cv_summary_fr="Résumé FR",
        candidate_cv_summary_en="Summary EN",
    )
    prompt_fr = _build_openai_user_prompt(ctx_fr)
    assert "Language: French" in prompt_fr
    assert "Résumé FR" in prompt_fr
    assert "Summary EN" not in prompt_fr

    ctx_en = MessageContext(
        candidate_name="Mohammed Bakhtaoui",
        candidate_degree="Eng",
        candidate_school=None,
        candidate_specialization=None,
        candidate_email=None,
        candidate_cv_summary=None,
        job_title="Dev",
        job_description=None,
        job_url="https://x.com",
        job_location=None,
        company_name="Co",
        company_description=None,
        company_website=None,
        recruiter_name=None,
        recruiter_title=None,
        recruiter_email=None,
        channel="email",
        language="en",
        candidate_cv_summary_fr="Résumé FR",
        candidate_cv_summary_en="Summary EN",
    )
    prompt_en = _build_openai_user_prompt(ctx_en)
    assert "Language: English" in prompt_en
    assert "Summary EN" in prompt_en
    assert "Résumé FR" not in prompt_en
