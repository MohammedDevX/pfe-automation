import asyncio
from unittest.mock import AsyncMock, MagicMock, patch


import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, Settings
from app.integrations.message_providers import (
    GeneratedMessage,
    MessageContext,
    OpenAIMessageProvider,
    TemplateMessageProvider,
)
from app.models import (
    Application,
    ApplicationStatus,
    Company,
    Contact,
    MessageChannel,
    MessageStatus,
    OutboundMessage,
)
from app.schemas import MessageGenerateRequest
from app.services.messaging import (
    approve_message,
    generate_message,
    select_best_candidate_project,
    send_message,
)
from app.services.research import get_or_create_company, upsert_contact, ContactCandidate


def make_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def make_application(title="Stage PFE Backend .NET", company_name="Acme Tech") -> Application:
    return Application(
        company=company_name,
        position=title,
        source="manual",
        job_url="https://acme.example.com/jobs/pfe",
        location="Casablanca, Morocco",
        description="Looking for a backend intern proficient in ASP.NET Core, C#, SQL Server, and Docker.",
        professional_email="hr@acme.example.com",
    )


def test_1_personalized_email_generation():
    session = make_session()
    app_obj = make_application()
    session.add(app_obj)
    session.commit()

    req = MessageGenerateRequest(channel=MessageChannel.email, provider="template", language="fr")
    msg = asyncio.run(generate_message(session, app_obj, req, Settings()))

    assert msg.channel == MessageChannel.email
    assert msg.status == MessageStatus.draft
    assert "Candidature Stage PFE" in msg.subject
    assert "Acme Tech" in msg.body
    assert "GearOil" in msg.body
    assert "ASP.NET Core" in msg.body
    assert msg.metadata_json["generation_version"] == "3.5.3"


def test_2_personalized_linkedin_message():
    session = make_session()
    app_obj = make_application()
    session.add(app_obj)
    session.commit()

    req = MessageGenerateRequest(channel=MessageChannel.linkedin, provider="template", language="fr")
    msg = asyncio.run(generate_message(session, app_obj, req, Settings()))

    assert msg.channel == MessageChannel.linkedin
    assert msg.subject is None
    assert "Acme Tech" in msg.body
    words = msg.body.split()
    assert 40 <= len(words) <= 120


def test_3_relevant_project_selection_dotnet():
    projects = Settings().get_candidate_projects()
    selected, signals = select_best_candidate_project(
        job_title="Stage PFE Backend .NET",
        job_description="Recherche développeur C# ASP.NET Core SQL Server Docker",
        job_tech_signals=["ASP.NET Core", "C#", "Docker"],
        company_tech_signals=["SQL Server"],
        candidate_projects=projects,
    )
    assert selected["name"] == "GearOil"
    assert "ASP.NET Core" in signals


def test_4_relevant_project_selection_php():
    projects = Settings().get_candidate_projects()
    selected, signals = select_best_candidate_project(
        job_title="Stage Développeur Fullstack PHP / React",
        job_description="Conception d'applications web avec Laravel et React.",
        job_tech_signals=["Laravel", "React", "PHP"],
        company_tech_signals=["MySQL"],
        candidate_projects=projects,
    )
    assert selected["name"] == "SmartMunicipality"
    assert "Laravel" in signals or "React" in signals


def test_5_technology_overlap_selection():
    projects = Settings().get_candidate_projects()
    selected, signals = select_best_candidate_project(
        job_title="Data Engineer Intern",
        job_description="Pipeline ETL Python Spark PyTorch Docker",
        job_tech_signals=["Python", "Spark", "PyTorch"],
        company_tech_signals=["Docker"],
        candidate_projects=projects,
    )
    assert selected["name"] == "DataPipelineX"
    assert "Python" in signals or "Spark" in signals


def test_6_french_language_generation():
    session = make_session()
    app_obj = make_application(title="Stage PFE Développeur Backend")
    session.add(app_obj)
    session.commit()

    req = MessageGenerateRequest(channel=MessageChannel.email, provider="template", language="fr")
    msg = asyncio.run(generate_message(session, app_obj, req, Settings()))

    assert "Bonjour" in msg.body or "Madame, Monsieur" in msg.body
    assert "Candidature Stage PFE" in msg.subject


def test_7_english_language_generation():
    session = make_session()
    app_obj = make_application(title="Software Engineer Intern PFE")
    app_obj.description = "We offer a 6-month final-year internship for software developers."
    session.add(app_obj)
    session.commit()

    req = MessageGenerateRequest(channel=MessageChannel.email, provider="template", language="en")
    msg = asyncio.run(generate_message(session, app_obj, req, Settings()))

    assert "Dear" in msg.body
    assert "Internship Application" in msg.subject



def test_8_recruiter_specific_wording():
    session = make_session()
    app_obj = make_application()
    session.add(app_obj)
    session.commit()

    company = get_or_create_company(session, app_obj)
    contact = upsert_contact(
        session,
        company,
        ContactCandidate(
            name="Sarah Recruiter",
            job_title="Talent Acquisition Partner",
            linkedin_url="https://www.linkedin.com/in/sarah-recruiter",
            professional_email="sarah@acme.example.com",
            email_verification_status="valid",
            email_confidence=0.9,
            source="test",
            source_url=None,
        ),
    )

    req = MessageGenerateRequest(channel=MessageChannel.email, contact_id=contact.id, provider="template")
    msg = asyncio.run(generate_message(session, app_obj, req, Settings()))

    assert "Bonjour Sarah Recruiter" in msg.body
    assert msg.metadata_json["contact_relevance"] == "HIGH"


def test_9_engineering_manager_specific_wording():
    session = make_session()
    app_obj = make_application()
    session.add(app_obj)
    session.commit()

    company = get_or_create_company(session, app_obj)
    contact = upsert_contact(
        session,
        company,
        ContactCandidate(
            name="Alex Manager",
            job_title="Engineering Manager",
            linkedin_url="https://www.linkedin.com/in/alex-manager",
            professional_email="alex@acme.example.com",
            email_verification_status="valid",
            email_confidence=0.9,
            source="test",
            source_url=None,
        ),
    )

    req = MessageGenerateRequest(channel=MessageChannel.email, contact_id=contact.id, provider="template")
    msg = asyncio.run(generate_message(session, app_obj, req, Settings()))

    assert "enjeux d'ingénierie" in msg.body or "engineering" in msg.body.lower()
    assert msg.metadata_json["contact_relevance"] == "MEDIUM"


def test_10_missing_contact_handling():
    session = make_session()
    app_obj = make_application()
    app_obj.recruiter = None
    session.add(app_obj)
    session.commit()

    req = MessageGenerateRequest(channel=MessageChannel.email, provider="template")
    msg = asyncio.run(generate_message(session, app_obj, req, Settings()))

    assert "Madame, Monsieur" in msg.body or "Dear" in msg.body
    assert msg.contact_id is None


def test_11_missing_professional_email_validation():
    session = make_session()
    app_obj = Application(
        company="Empty Co",
        position="Backend Intern",
        source="manual",
        job_url="https://empty.example.com",
    )
    session.add(app_obj)
    session.commit()

    req = MessageGenerateRequest(channel=MessageChannel.email, provider="template")
    with pytest.raises(ValueError, match="No professional recipient email found"):
        asyncio.run(generate_message(session, app_obj, req, Settings()))


def test_12_no_fabricated_facts():
    session = make_session()
    app_obj = make_application()
    session.add(app_obj)
    session.commit()

    settings = Settings(
        candidate_first_name="Mehdi",
        candidate_last_name="Benzine",
        candidate_school="ENSIAS",
    )

    req = MessageGenerateRequest(channel=MessageChannel.email, provider="template")
    msg = asyncio.run(generate_message(session, app_obj, req, settings))

    assert "ENSIAS" in msg.body
    assert "Mehdi Benzine" in msg.body
    # Verify no fake awards or unprovided companies
    assert "passionate about your company" not in msg.body.lower()


def test_13_no_fabricated_urls():
    session = make_session()
    app_obj = make_application()
    session.add(app_obj)
    session.commit()

    settings = Settings(candidate_github_url=None, candidate_linkedin_url=None)

    req = MessageGenerateRequest(channel=MessageChannel.email, provider="template")
    msg = asyncio.run(generate_message(session, app_obj, req, settings))

    assert "github.com" not in msg.body.lower()
    assert "linkedin.com" not in msg.body.lower()


def test_14_template_provider_fallback_without_openai():
    session = make_session()
    app_obj = make_application()
    session.add(app_obj)
    session.commit()

    settings = Settings(openai_api_key=None)

    req = MessageGenerateRequest(channel=MessageChannel.email, provider="template")
    msg = asyncio.run(generate_message(session, app_obj, req, settings))

    assert msg.generation_provider == "template"
    assert msg.status == MessageStatus.draft


def test_15_openai_provider_uses_structured_context():
    session = make_session()
    app_obj = make_application()
    session.add(app_obj)
    session.commit()

    settings = Settings(openai_api_key="test-sk-key")

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "model": "gpt-4o-mini",
        "choices": [
            {
                "message": {
                    "content": "SUBJECT: Candidature PFE – Backend .NET\nBODY:\nBonjour,\n\nMessage body content from LLM."
                }
            }
        ],
    }

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_response) as mock_post:
        req = MessageGenerateRequest(channel=MessageChannel.email, provider="openai")
        msg = asyncio.run(generate_message(session, app_obj, req, settings))

        assert msg.generation_provider == "openai"
        assert msg.subject == "Candidature PFE – Backend .NET"
        assert "Message body content from LLM." in msg.body
        assert mock_post.called



def test_16_regeneration_preserves_previous_messages():
    session = make_session()
    app_obj = make_application()
    session.add(app_obj)
    session.commit()

    req = MessageGenerateRequest(channel=MessageChannel.email, provider="template")
    msg1 = asyncio.run(generate_message(session, app_obj, req, Settings()))
    approve_message(session, msg1)

    req_regen = MessageGenerateRequest(channel=MessageChannel.email, provider="template", regenerate=True)
    msg2 = asyncio.run(generate_message(session, app_obj, req_regen, Settings()))

    messages_in_db = session.query(OutboundMessage).filter_by(application_id=app_obj.id).all()
    assert len(messages_in_db) == 2
    assert msg1.status == MessageStatus.approved
    assert msg2.status == MessageStatus.draft


def test_17_generated_messages_remain_draft():
    session = make_session()
    app_obj = make_application()
    session.add(app_obj)
    session.commit()

    req = MessageGenerateRequest(channel=MessageChannel.email, provider="template")
    msg = asyncio.run(generate_message(session, app_obj, req, Settings()))

    assert msg.status == MessageStatus.draft
    assert msg.sent_at is None


def test_18_existing_messaging_behavior_unchanged():
    session = make_session()
    app_obj = make_application()
    session.add(app_obj)
    session.commit()

    req = MessageGenerateRequest(channel=MessageChannel.email, provider="template")
    msg = asyncio.run(generate_message(session, app_obj, req, Settings()))

    approved = approve_message(session, msg)
    assert approved.status == MessageStatus.approved

    # Test send in dry-run mode
    settings = Settings()
    sent_msg = asyncio.run(send_message(session, approved, settings, to_email="test@acme.com"))
    assert sent_msg.status == MessageStatus.approved  # Dry run leaves as approved with dry-run note
    assert "[DRY RUN]" in sent_msg.failure_reason
