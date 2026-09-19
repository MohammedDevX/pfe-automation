import asyncio

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, Settings
from app.models import Application, EmailKind
from app.schemas import ResearchRequest
from app.services.research import (
    ContactCandidate,
    EmailCandidate,
    classify_email,
    get_or_create_company,
    normalize_company_name,
    research_application,
    upsert_contact,
    upsert_email,
)


def make_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def make_application() -> Application:
    return Application(
        company="Example SARL",
        position="PFE Backend Developer",
        source="manual",
        job_url="https://example.com/jobs/pfe",
        location="Casablanca",
    )


def test_company_normalization_and_deduplication() -> None:
    session = make_session()
    app1 = make_application()
    app2 = make_application()
    app2.company = "Example"
    app2.job_url = "https://example.com/jobs/pfe-2"
    session.add_all([app1, app2])
    session.commit()

    company1 = get_or_create_company(session, app1)
    company2 = get_or_create_company(session, app2)

    assert normalize_company_name("Example SARL") == "example"
    assert company1.id == company2.id


def test_recruiter_deduplication_by_email_and_persists_confidence_source() -> None:
    session = make_session()
    app = make_application()
    session.add(app)
    session.commit()
    company = get_or_create_company(session, app)

    candidate = ContactCandidate(
        name="Jane Doe",
        job_title="Talent Acquisition Specialist",
        linkedin_url=None,
        professional_email="jane@example.com",
        email_verification_status="valid",
        email_confidence=0.92,
        source="hunter",
        source_url="https://hunter.io/search/example.com",
    )

    first = upsert_contact(session, company, candidate)
    second = upsert_contact(session, company, candidate)

    assert first.id == second.id
    assert second.email_confidence == 0.92
    assert second.source == "hunter"
    assert second.source_url == "https://hunter.io/search/example.com"


def test_email_classification() -> None:
    assert classify_email("jane@example.com", "example.com", verified=True) == EmailKind.verified_individual
    assert classify_email("jane@example.com", "example.com") == EmailKind.unverified_individual
    assert classify_email("contact@example.com", "example.com") == EmailKind.generic
    assert classify_email("someone@gmail.com", "example.com") == EmailKind.none
    assert classify_email("jane@other.com", "example.com") == EmailKind.none


def test_email_confidence_and_source_persistence() -> None:
    session = make_session()
    app = make_application()
    session.add(app)
    session.commit()
    company = get_or_create_company(session, app)

    email = upsert_email(
        session,
        company,
        EmailCandidate(
            address="hr@example.com",
            kind=EmailKind.generic,
            provider="public_website",
            verification_status="unverified",
            confidence=0.55,
            source_url="https://example.com/contact",
        ),
    )

    assert email.address == "hr@example.com"
    assert email.kind == EmailKind.generic
    assert email.provider == "public_website"
    assert email.confidence == 0.55
    assert email.source_url == "https://example.com/contact"


def test_missing_hunter_credentials_are_captured() -> None:
    session = make_session()
    app = make_application()
    session.add(app)
    session.commit()

    result = asyncio.run(
        research_application(
            session,
            app,
            ResearchRequest(providers=["hunter"], website="https://example.com"),
            Settings(hunter_api_key=None),
        )
    )

    assert result.company.normalized_name == "example"
    assert result.provider_errors == ["Hunter requires HUNTER_API_KEY."]


def test_provider_without_company_website_is_captured() -> None:
    session = make_session()
    app = make_application()
    app.job_url = "https://adzuna.example/jobs/1"
    session.add(app)
    session.commit()

    result = asyncio.run(
        research_application(
            session,
            app,
            ResearchRequest(providers=["hunter"]),
            Settings(hunter_api_key="test-api-key"),
        )
    )

    assert result.provider_errors == ["Hunter requires a company website/domain."]
