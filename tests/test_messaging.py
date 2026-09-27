"""Tests for message generation, review lifecycle, sending, and tracking.

All external API calls (OpenAI, SMTP) are mocked — no real network requests.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
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
    IncomingResponse,
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


# ---------------------------------------------------------------------------
# Phase 3.6.1 & 3.6.2 Regression Tests
# ---------------------------------------------------------------------------


def test_generate_followup_sets_follow_up_count_in_context() -> None:
    session = make_session()
    app = make_application(session)
    app.follow_up_count = 1
    session.commit()

    sent_msg = OutboundMessage(
        application_id=app.id,
        channel=MessageChannel.email,
        subject="Initial outreach",
        body="Initial message body text",
        status=MessageStatus.sent,
    )
    session.add(sent_msg)
    session.commit()

    with patch("app.services.messaging.build_message_provider") as mock_build:
        mock_provider = MagicMock()
        mock_provider.generate = AsyncMock(return_value=GeneratedMessage(subject="Relance", body="Relance body", provider="mock"))
        mock_build.return_value = mock_provider

        asyncio.run(generate_message(session, app, MessageGenerateRequest(channel=MessageChannel.email), DEFAULT_SETTINGS))

        mock_provider.generate.assert_called_once()
        ctx: MessageContext = mock_provider.generate.call_args[0][0]
        assert ctx.follow_up_count == 1
        assert ctx.previous_body == "Initial message body text"


def test_initial_message_has_zero_follow_up_count() -> None:
    session = make_session()
    app = make_application(session)
    assert app.follow_up_count == 0

    with patch("app.services.messaging.build_message_provider") as mock_build:
        mock_provider = MagicMock()
        mock_provider.generate = AsyncMock(return_value=GeneratedMessage(subject="Subject", body="Body", provider="mock"))
        mock_build.return_value = mock_provider

        asyncio.run(generate_message(session, app, MessageGenerateRequest(channel=MessageChannel.email), DEFAULT_SETTINGS))

        ctx: MessageContext = mock_provider.generate.call_args[0][0]
        assert ctx.follow_up_count == 0
        assert ctx.previous_body is None


def test_followup_uses_previous_sent_message() -> None:
    session = make_session()
    app = make_application(session)
    app.follow_up_count = 1

    msg1 = OutboundMessage(
        application_id=app.id,
        channel=MessageChannel.email,
        subject="Subject 1",
        body="First sent email content",
        status=MessageStatus.sent,
    )
    session.add(msg1)
    session.commit()

    with patch("app.services.messaging.build_message_provider") as mock_build:
        mock_provider = MagicMock()
        mock_provider.generate = AsyncMock(return_value=GeneratedMessage(subject="Subject 2", body="Followup body", provider="mock"))
        mock_build.return_value = mock_provider

        asyncio.run(generate_message(session, app, MessageGenerateRequest(channel=MessageChannel.email), DEFAULT_SETTINGS))

        ctx: MessageContext = mock_provider.generate.call_args[0][0]
        assert ctx.previous_body == "First sent email content"


def test_non_sent_message_is_not_used_as_previous_body() -> None:
    session = make_session()
    app = make_application(session)
    app.follow_up_count = 1

    draft_msg = OutboundMessage(
        application_id=app.id,
        channel=MessageChannel.email,
        subject="Draft",
        body="Draft body",
        status=MessageStatus.draft,
    )
    failed_msg = OutboundMessage(
        application_id=app.id,
        channel=MessageChannel.email,
        subject="Failed",
        body="Failed body",
        status=MessageStatus.failed,
    )
    rejected_msg = OutboundMessage(
        application_id=app.id,
        channel=MessageChannel.email,
        subject="Rejected",
        body="Rejected body",
        status=MessageStatus.rejected,
    )
    session.add_all([draft_msg, failed_msg, rejected_msg])
    session.commit()

    with patch("app.services.messaging.build_message_provider") as mock_build:
        mock_provider = MagicMock()
        mock_provider.generate = AsyncMock(return_value=GeneratedMessage(subject="Sub", body="Body", provider="mock"))
        mock_build.return_value = mock_provider

        asyncio.run(generate_message(session, app, MessageGenerateRequest(channel=MessageChannel.email, regenerate=True), DEFAULT_SETTINGS))

        ctx: MessageContext = mock_provider.generate.call_args[0][0]
        assert ctx.previous_body is None


def test_send_transitions_discovered_to_contacted() -> None:
    session = make_session()
    app = make_application(session)
    app.application_status = ApplicationStatus.discovered
    msg = make_draft(session, app)
    approve_message(session, msg)

    null_sender = NullEmailSender()
    with patch("app.services.messaging.build_email_sender", return_value=null_sender):
        asyncio.run(send_message(session, msg, DEFAULT_SETTINGS, to_email="test@example.com"))

    session.refresh(app)
    assert app.application_status == ApplicationStatus.contacted


def test_send_transitions_qualified_to_contacted() -> None:
    session = make_session()
    app = make_application(session)
    app.application_status = ApplicationStatus.qualified
    msg = make_draft(session, app)
    approve_message(session, msg)

    null_sender = NullEmailSender()
    with patch("app.services.messaging.build_email_sender", return_value=null_sender):
        asyncio.run(send_message(session, msg, DEFAULT_SETTINGS, to_email="test@example.com"))

    session.refresh(app)
    assert app.application_status == ApplicationStatus.contacted


def test_send_transitions_researched_to_contacted() -> None:
    session = make_session()
    app = make_application(session)
    app.application_status = ApplicationStatus.researched
    msg = make_draft(session, app)
    approve_message(session, msg)

    null_sender = NullEmailSender()
    with patch("app.services.messaging.build_email_sender", return_value=null_sender):
        asyncio.run(send_message(session, msg, DEFAULT_SETTINGS, to_email="test@example.com"))

    session.refresh(app)
    assert app.application_status == ApplicationStatus.contacted


def test_send_transitions_contact_ready_to_contacted() -> None:
    session = make_session()
    app = make_application(session)
    app.application_status = ApplicationStatus.contact_ready
    msg = make_draft(session, app)
    approve_message(session, msg)

    null_sender = NullEmailSender()
    with patch("app.services.messaging.build_email_sender", return_value=null_sender):
        asyncio.run(send_message(session, msg, DEFAULT_SETTINGS, to_email="test@example.com"))

    session.refresh(app)
    assert app.application_status == ApplicationStatus.contacted


def test_failed_send_does_not_transition_to_contacted() -> None:
    session = make_session()
    app = make_application(session)
    app.application_status = ApplicationStatus.contact_ready
    msg = make_draft(session, app)
    approve_message(session, msg)

    failing_sender = NullEmailSender()
    async def bad_send(**kwargs):
        raise EmailSendError("SMTP Error")
    failing_sender.send = bad_send

    with patch("app.services.messaging.build_email_sender", return_value=failing_sender):
        asyncio.run(send_message(session, msg, DEFAULT_SETTINGS, to_email="test@example.com"))

    session.refresh(app)
    assert app.application_status == ApplicationStatus.contact_ready
    assert msg.status == MessageStatus.failed


def test_dry_run_does_not_transition_to_contacted() -> None:
    session = make_session()
    app = make_application(session)
    app.application_status = ApplicationStatus.contact_ready
    msg = make_draft(session, app)
    approve_message(session, msg)

    dry_run_settings = Settings(
        dry_run_email=True,
        candidate_first_name="Alice",
        candidate_last_name="Martin",
        candidate_degree="student",
        candidate_school="ENSIAS",
        candidate_specialization="SE",
        candidate_email="alice@example.com",
    )

    asyncio.run(send_message(session, msg, dry_run_settings, to_email="test@example.com"))

    session.refresh(app)
    assert app.application_status == ApplicationStatus.contact_ready
    assert msg.status == MessageStatus.approved
    assert "[DRY RUN]" in msg.failure_reason


def test_approval_does_not_transition_to_contacted() -> None:
    session = make_session()
    app = make_application(session)
    app.application_status = ApplicationStatus.contact_ready
    msg = make_draft(session, app)

    approve_message(session, msg)

    session.refresh(app)
    assert app.application_status == ApplicationStatus.contact_ready


def test_generation_does_not_transition_to_contacted() -> None:
    session = make_session()
    app = make_application(session)
    app.application_status = ApplicationStatus.contact_ready

    asyncio.run(generate_message(session, app, MessageGenerateRequest(channel=MessageChannel.email), DEFAULT_SETTINGS))

    session.refresh(app)
    assert app.application_status == ApplicationStatus.contact_ready


# ---------------------------------------------------------------------------
# Phase 3.6.4 Idempotency Tests
# ---------------------------------------------------------------------------


def test_generate_message_idempotent_returns_existing_draft_when_regenerate_false() -> None:
    session = make_session()
    app = make_application(session)

    with patch("app.services.messaging.build_message_provider") as mock_build:
        mock_provider = MagicMock()
        mock_provider.generate = AsyncMock(return_value=GeneratedMessage(subject="Sub", body="Body 1", provider="mock"))
        mock_build.return_value = mock_provider

        msg1 = asyncio.run(generate_message(session, app, MessageGenerateRequest(channel=MessageChannel.email, regenerate=False), DEFAULT_SETTINGS))
        assert mock_provider.generate.call_count == 1

        # Second call with regenerate=False should return the exact same draft without calling provider again
        msg2 = asyncio.run(generate_message(session, app, MessageGenerateRequest(channel=MessageChannel.email, regenerate=False), DEFAULT_SETTINGS))
        assert mock_provider.generate.call_count == 1
        assert msg2.id == msg1.id
        assert msg2.body == "Body 1"

        # Verify database row count did not increase
        messages = list(session.scalars(select(OutboundMessage).where(OutboundMessage.application_id == app.id)))
        assert len(messages) == 1


def test_generate_message_regenerate_true_rejects_old_draft_and_creates_new() -> None:
    session = make_session()
    app = make_application(session)

    msg1 = make_draft(session, app)
    assert msg1.status == MessageStatus.draft

    with patch("app.services.messaging.build_message_provider") as mock_build:
        mock_provider = MagicMock()
        mock_provider.generate = AsyncMock(return_value=GeneratedMessage(subject="Sub 2", body="Body 2", provider="mock"))
        mock_build.return_value = mock_provider

        msg2 = asyncio.run(generate_message(session, app, MessageGenerateRequest(channel=MessageChannel.email, regenerate=True), DEFAULT_SETTINGS))

    session.refresh(msg1)
    assert msg1.status == MessageStatus.rejected
    assert msg2.id != msg1.id
    assert msg2.status == MessageStatus.draft
    assert msg2.body == "Body 2"

    messages = list(session.scalars(select(OutboundMessage).where(OutboundMessage.application_id == app.id)))
    assert len(messages) == 2


def test_generate_message_raises_when_approved_message_exists() -> None:
    session = make_session()
    app = make_application(session)
    msg = make_draft(session, app)
    approve_message(session, msg)
    assert msg.status == MessageStatus.approved

    with pytest.raises(ValueError, match="approved message already exists"):
        asyncio.run(generate_message(session, app, MessageGenerateRequest(channel=MessageChannel.email, regenerate=False), DEFAULT_SETTINGS))


def test_generate_message_allows_new_draft_after_previous_sent() -> None:
    session = make_session()
    app = make_application(session)
    sent_msg = OutboundMessage(
        application_id=app.id,
        channel=MessageChannel.email,
        subject="Sent subject",
        body="Sent body",
        status=MessageStatus.sent,
    )
    session.add(sent_msg)
    session.commit()

    with patch("app.services.messaging.build_message_provider") as mock_build:
        mock_provider = MagicMock()
        mock_provider.generate = AsyncMock(return_value=GeneratedMessage(subject="New sub", body="New body", provider="mock"))
        mock_build.return_value = mock_provider

        new_msg = asyncio.run(generate_message(session, app, MessageGenerateRequest(channel=MessageChannel.email, regenerate=False), DEFAULT_SETTINGS))

    assert new_msg.id != sent_msg.id
    assert new_msg.status == MessageStatus.draft


def test_generate_message_allows_new_draft_after_previous_rejected() -> None:
    session = make_session()
    app = make_application(session)
    rejected_msg = OutboundMessage(
        application_id=app.id,
        channel=MessageChannel.email,
        subject="Rejected subject",
        body="Rejected body",
        status=MessageStatus.rejected,
    )
    session.add(rejected_msg)
    session.commit()

    with patch("app.services.messaging.build_message_provider") as mock_build:
        mock_provider = MagicMock()
        mock_provider.generate = AsyncMock(return_value=GeneratedMessage(subject="New sub", body="New body", provider="mock"))
        mock_build.return_value = mock_provider

        new_msg = asyncio.run(generate_message(session, app, MessageGenerateRequest(channel=MessageChannel.email, regenerate=False), DEFAULT_SETTINGS))

    assert new_msg.id != rejected_msg.id
    assert new_msg.status == MessageStatus.draft


def test_generate_message_allows_new_draft_after_previous_failed() -> None:
    session = make_session()
    app = make_application(session)
    failed_msg = OutboundMessage(
        application_id=app.id,
        channel=MessageChannel.email,
        subject="Failed subject",
        body="Failed body",
        status=MessageStatus.failed,
    )
    session.add(failed_msg)
    session.commit()

    with patch("app.services.messaging.build_message_provider") as mock_build:
        mock_provider = MagicMock()
        mock_provider.generate = AsyncMock(return_value=GeneratedMessage(subject="New sub", body="New body", provider="mock"))
        mock_build.return_value = mock_provider

        new_msg = asyncio.run(generate_message(session, app, MessageGenerateRequest(channel=MessageChannel.email, regenerate=False), DEFAULT_SETTINGS))

    assert new_msg.id != failed_msg.id
    assert new_msg.status == MessageStatus.draft


def test_followup_generation_idempotent_when_draft_exists() -> None:
    session = make_session()
    app = make_application(session)
    app.follow_up_count = 1

    sent_msg = OutboundMessage(
        application_id=app.id,
        channel=MessageChannel.email,
        subject="Sent",
        body="Initial sent body",
        status=MessageStatus.sent,
    )
    session.add(sent_msg)
    session.commit()

    with patch("app.services.messaging.build_message_provider") as mock_build:
        mock_provider = MagicMock()
        mock_provider.generate = AsyncMock(return_value=GeneratedMessage(subject="Followup", body="Followup draft body", provider="mock"))
        mock_build.return_value = mock_provider

        msg1 = asyncio.run(generate_message(session, app, MessageGenerateRequest(channel=MessageChannel.email, regenerate=False), DEFAULT_SETTINGS))
        assert mock_provider.generate.call_count == 1

        msg2 = asyncio.run(generate_message(session, app, MessageGenerateRequest(channel=MessageChannel.email, regenerate=False), DEFAULT_SETTINGS))
        assert mock_provider.generate.call_count == 1
        assert msg2.id == msg1.id


def test_regenerate_approved_message_is_rejected() -> None:
    session = make_session()
    app = make_application(session)
    msg = make_draft(session, app)
    approve_message(session, msg)

    with pytest.raises(ValueError, match="approved message already exists"):
        asyncio.run(generate_message(session, app, MessageGenerateRequest(channel=MessageChannel.email, regenerate=True), DEFAULT_SETTINGS))


def test_provider_is_not_called_when_existing_draft_is_returned() -> None:
    session = make_session()
    app = make_application(session)
    msg = make_draft(session, app)

    with patch("app.services.messaging.build_message_provider") as mock_build:
        mock_provider = MagicMock()
        mock_build.return_value = mock_provider

        result = asyncio.run(generate_message(session, app, MessageGenerateRequest(channel=MessageChannel.email, regenerate=False), DEFAULT_SETTINGS))
        assert result.id == msg.id
        mock_provider.generate.assert_not_called()


def test_provider_can_be_called_again_after_regeneration() -> None:
    session = make_session()
    app = make_application(session)
    msg1 = make_draft(session, app)

    with patch("app.services.messaging.build_message_provider") as mock_build:
        mock_provider = MagicMock()
        mock_provider.generate = AsyncMock(return_value=GeneratedMessage(subject="Sub 2", body="Body 2", provider="mock"))
        mock_build.return_value = mock_provider

        msg2 = asyncio.run(generate_message(session, app, MessageGenerateRequest(channel=MessageChannel.email, regenerate=True), DEFAULT_SETTINGS))
        assert mock_provider.generate.call_count == 1
        assert msg2.id != msg1.id


def test_historical_duplicate_drafts_uses_most_recent_deterministically() -> None:
    session = make_session()
    app = make_application(session)

    draft1 = OutboundMessage(
        application_id=app.id,
        channel=MessageChannel.email,
        subject="Draft 1",
        body="Older draft",
        status=MessageStatus.draft,
    )
    draft2 = OutboundMessage(
        application_id=app.id,
        channel=MessageChannel.email,
        subject="Draft 2",
        body="Newer draft",
        status=MessageStatus.draft,
    )
    session.add(draft1)
    session.add(draft2)
    session.commit()

    result = asyncio.run(generate_message(session, app, MessageGenerateRequest(channel=MessageChannel.email, regenerate=False), DEFAULT_SETTINGS))
    assert result.id == draft2.id
    assert result.body == "Newer draft"


# ---------------------------------------------------------------------------
# Phase 3.6.3 Message-ID & Email Threading Tests
# ---------------------------------------------------------------------------


def test_outbound_email_gets_unique_message_id_stored() -> None:
    session = make_session()
    app = make_application(session)
    msg = asyncio.run(generate_message(session, app, MessageGenerateRequest(channel=MessageChannel.email), DEFAULT_SETTINGS))

    assert msg.sent_message_id is not None
    assert msg.sent_message_id.startswith("<outbound-")
    assert msg.sent_message_id.endswith(">")


def test_message_id_domain_uses_configured_sender() -> None:
    session = make_session()
    app = make_application(session)
    custom_settings = Settings(
        smtp_user="recruiter-outreach@company-domain.com",
        candidate_first_name="Alice",
        candidate_last_name="Martin",
    )
    msg = asyncio.run(generate_message(session, app, MessageGenerateRequest(channel=MessageChannel.email), custom_settings))

    assert msg.sent_message_id is not None
    assert "@company-domain.com>" in msg.sent_message_id


def test_smtp_sender_sets_threading_headers() -> None:
    sender = SMTPEmailSender(Settings(smtp_user="test@company.com", smtp_password="secret-app-pass"))
    with patch("smtplib.SMTP") as mock_smtp_cls:
        mock_server = MagicMock()
        mock_smtp_cls.return_value.__enter__.return_value = mock_server

        asyncio.run(
            sender.send(
                to="recruiter@target.com",
                subject="Application",
                body="Hello",
                from_addr="test@company.com",
                message_id="<msg-123@company.com>",
                in_reply_to="<parent-456@company.com>",
                references="<msg-000@company.com> <parent-456@company.com>",
            )
        )

        mock_server.sendmail.assert_called_once()
        raw_msg_str = mock_server.sendmail.call_args[0][2]
        assert "Message-ID: <msg-123@company.com>" in raw_msg_str
        assert "In-Reply-To: <parent-456@company.com>" in raw_msg_str
        assert "References: <msg-000@company.com> <parent-456@company.com>" in raw_msg_str


def test_smtp_sender_omits_none_headers() -> None:
    sender = SMTPEmailSender(Settings(smtp_user="test@company.com", smtp_password="secret-app-pass"))
    with patch("smtplib.SMTP") as mock_smtp_cls:
        mock_server = MagicMock()
        mock_smtp_cls.return_value.__enter__.return_value = mock_server

        asyncio.run(
            sender.send(
                to="recruiter@target.com",
                subject="Application",
                body="Hello",
                from_addr="test@company.com",
                message_id=None,
                in_reply_to=None,
                references=None,
            )
        )

        raw_msg_str = mock_server.sendmail.call_args[0][2]
        assert "In-Reply-To:" not in raw_msg_str
        assert "References:" not in raw_msg_str


def test_followup_threading_headers_chain_correctly() -> None:
    session = make_session()
    app = make_application(session)

    # Initial outreach sent
    msg1 = asyncio.run(generate_message(session, app, MessageGenerateRequest(channel=MessageChannel.email), DEFAULT_SETTINGS))
    approve_message(session, msg1)
    null_sender = NullEmailSender()
    with patch("app.services.messaging.build_email_sender", return_value=null_sender):
        asyncio.run(send_message(session, msg1, DEFAULT_SETTINGS, to_email="recruiter@tech.com"))
    assert msg1.status == MessageStatus.sent
    assert msg1.sent_message_id is not None
    assert msg1.in_reply_to is None

    # Follow-up #1
    app.follow_up_count = 1
    session.commit()
    msg2 = asyncio.run(generate_message(session, app, MessageGenerateRequest(channel=MessageChannel.email), DEFAULT_SETTINGS))
    assert msg2.in_reply_to == msg1.sent_message_id
    assert msg2.references_header == msg1.sent_message_id
    assert msg2.sent_message_id not in msg2.references_header

    approve_message(session, msg2)
    with patch("app.services.messaging.build_email_sender", return_value=null_sender):
        asyncio.run(send_message(session, msg2, DEFAULT_SETTINGS, to_email="recruiter@tech.com"))
    assert msg2.status == MessageStatus.sent

    # Follow-up #2
    app.follow_up_count = 2
    session.commit()
    msg3 = asyncio.run(generate_message(session, app, MessageGenerateRequest(channel=MessageChannel.email), DEFAULT_SETTINGS))
    assert msg3.in_reply_to == msg2.sent_message_id
    assert msg3.references_header == f"{msg1.sent_message_id} {msg2.sent_message_id}"
    assert msg3.sent_message_id not in msg3.references_header


def test_dry_run_persists_message_id_without_marking_sent() -> None:
    session = make_session()
    app = make_application(session)
    msg = asyncio.run(generate_message(session, app, MessageGenerateRequest(channel=MessageChannel.email), DEFAULT_SETTINGS))
    approve_message(session, msg)

    dry_run_settings = Settings(
        dry_run_email=True,
        candidate_first_name="Alice",
        candidate_last_name="Martin",
    )
    asyncio.run(send_message(session, msg, dry_run_settings, to_email="recruiter@tech.com"))

    session.refresh(msg)
    session.refresh(app)
    assert msg.sent_message_id is not None
    assert msg.status == MessageStatus.approved  # not sent
    assert app.application_status != ApplicationStatus.contacted  # not contacted


def test_imap_reader_extracts_threading_headers() -> None:
    from app.integrations.imap_reader import EmailCandidate, fetch_replies
    mock_conn = MagicMock()
    mock_conn.search.return_value = ("OK", [b"1"])
    mock_raw_email = (
        b"From: recruiter@corp.com\r\n"
        b"Subject: Re: Candidature PFE\r\n"
        b"Message-ID: <reply-100@corp.com>\r\n"
        b"In-Reply-To: <outbound-001@company.com>\r\n"
        b"References: <outbound-001@company.com>\r\n"
        b"Date: Mon, 25 Sep 2026 10:00:00 +0000\r\n"
        b"\r\n"
        b"Nous sommes interesser par votre profil."
    )
    mock_conn.fetch.return_value = ("OK", [(b"1", mock_raw_email)])

    with patch("imaplib.IMAP4_SSL", return_value=mock_conn):
        candidates = fetch_replies("imap.test.com", 993, "user@test.com", "pass")

    assert len(candidates) == 1
    c = candidates[0]
    assert c.message_id == "<reply-100@corp.com>"
    assert c.in_reply_to == "<outbound-001@company.com>"
    assert c.references == "<outbound-001@company.com>"


def test_auto_match_by_in_reply_to_header() -> None:
    from app.services.lifecycle import record_response
    from app.schemas import RecordResponseRequest

    session = make_session()
    app = make_application(session, company="AutoMatch Corp")
    msg = asyncio.run(generate_message(session, app, MessageGenerateRequest(channel=MessageChannel.email), DEFAULT_SETTINGS))
    approve_message(session, msg)

    null_sender = NullEmailSender()
    with patch("app.services.messaging.build_email_sender", return_value=null_sender):
        asyncio.run(send_message(session, msg, DEFAULT_SETTINGS, to_email="recruiter@automatch.com"))

    # Unrelated dummy application to test matching against correct app
    other_app = make_application(session, company="Other Corp")

    rec_req = RecordResponseRequest(
        sender="recruiter@automatch.com",
        subject="Re: Candidature PFE",
        body_preview="Oui, disponibilite mardi.",
        message_id_header="<reply-999@automatch.com>",
        in_reply_to_header=msg.sent_message_id,
        source="imap",
    )

    # Pass other_app as default, but in_reply_to_header should auto-match to app.id!
    response = record_response(session, other_app, rec_req)

    assert response.application_id == app.id
    assert response.in_reply_to_header == msg.sent_message_id


def test_unknown_in_reply_to_does_not_auto_match() -> None:
    from app.services.lifecycle import record_response
    from app.schemas import RecordResponseRequest

    session = make_session()
    app = make_application(session, company="Manual Match Corp")

    rec_req = RecordResponseRequest(
        sender="unknown@corp.com",
        subject="Hello",
        body_preview="Random email",
        message_id_header="<random-mid@corp.com>",
        in_reply_to_header="<unknown-outbound-id@nonexistent.com>",
        source="imap",
    )

    response = record_response(session, app, rec_req)
    assert response.application_id == app.id  # preserved provided application
    assert response.in_reply_to_header == "<unknown-outbound-id@nonexistent.com>"


def test_additive_schema_columns_support_historical_nulls() -> None:
    from app.main import ensure_additive_columns
    session = make_session()
    ensure_additive_columns()

    # Create historical OutboundMessage & IncomingResponse with NULL threading fields
    app = make_application(session)
    old_msg = OutboundMessage(
        application_id=app.id,
        channel=MessageChannel.email,
        body="Old message",
        status=MessageStatus.sent,
        sent_message_id=None,
        in_reply_to=None,
        references_header=None,
    )
    old_resp = IncomingResponse(
        application_id=app.id,
        received_at=datetime.utcnow(),
        sender="old@recruiter.com",
        message_id_header="<old-msg-id@recruiter.com>",
        in_reply_to_header=None,
    )
    session.add(old_msg)
    session.add(old_resp)
    session.commit()

    # Ensure they can be queried without error
    loaded_msg = session.get(OutboundMessage, old_msg.id)
    loaded_resp = session.get(IncomingResponse, old_resp.id)
    assert loaded_msg.sent_message_id is None
    assert loaded_resp.in_reply_to_header is None



