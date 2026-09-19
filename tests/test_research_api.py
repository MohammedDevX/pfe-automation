"""Tests for company research API endpoints and edge cases."""
import asyncio
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, Settings
from app.integrations.research_providers import ProviderResearchResult
from app.models import Application, Company, Contact, EmailKind, ProfessionalEmail
from app.schemas import ResearchRequest
from app.services.research import (
    classify_email,
    get_or_create_company,
    infer_company_website,
    normalize_company_name,
    upsert_contact,
    upsert_email,
    ContactCandidate,
    EmailCandidate,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_memory_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def make_application(
    company: str = "TechCorp SARL",
    job_url: str = "https://techcorp.ma/jobs/pfe",
    location: str = "Casablanca",
) -> Application:
    return Application(
        company=company,
        position="PFE Backend Developer",
        source="manual",
        job_url=job_url,
        location=location,
    )


# ---------------------------------------------------------------------------
# Unit tests — normalization, classification, helpers
# ---------------------------------------------------------------------------


def test_normalize_removes_legal_suffixes() -> None:
    assert normalize_company_name("Acme Inc.") == "acme"
    assert normalize_company_name("Société SAS") == "société"
    assert normalize_company_name("Startup SARL") == "startup"
    assert normalize_company_name("Global Ltd") == "global"


def test_infer_website_from_direct_url() -> None:
    assert infer_company_website("https://techcorp.ma/jobs/1") == "https://techcorp.ma"


def test_infer_website_skips_known_ats_domains() -> None:
    assert infer_company_website("https://jobs.lever.co/acme/1234") is None
    assert infer_company_website("https://boards.greenhouse.io/acme") is None
    assert infer_company_website("https://www.adzuna.ma/jobs/ad/1") is None


def test_classify_email_verified_vs_unverified() -> None:
    assert classify_email("alice@techcorp.ma", "techcorp.ma", verified=True) == EmailKind.verified_individual
    assert classify_email("alice@techcorp.ma", "techcorp.ma", verified=False) == EmailKind.unverified_individual


def test_classify_email_generic_prefixes() -> None:
    for prefix in ("info", "contact", "jobs", "careers", "rh", "hr", "recruitment"):
        assert classify_email(f"{prefix}@techcorp.ma", "techcorp.ma") == EmailKind.generic, prefix


def test_classify_email_free_domains_return_none() -> None:
    for domain in ("gmail.com", "yahoo.com", "hotmail.com", "outlook.com"):
        assert classify_email(f"alice@{domain}", "techcorp.ma") == EmailKind.none, domain


def test_classify_email_cross_domain_returns_none() -> None:
    assert classify_email("alice@othercorp.com", "techcorp.ma") == EmailKind.none


# ---------------------------------------------------------------------------
# Unit tests — get_or_create_company
# ---------------------------------------------------------------------------


def test_get_or_create_creates_company_with_inferred_website() -> None:
    session = make_memory_session()
    app = make_application(job_url="https://techcorp.ma/jobs/pfe")
    session.add(app)
    session.commit()

    company = get_or_create_company(session, app)

    assert company.id is not None
    assert company.name == "TechCorp SARL"
    assert company.normalized_name == "techcorp"
    assert company.website == "https://techcorp.ma"
    assert company.location == "Casablanca"


def test_get_or_create_returns_existing_for_normalized_match() -> None:
    session = make_memory_session()
    app1 = make_application(company="TechCorp SARL")
    app2 = make_application(company="TechCorp", job_url="https://techcorp.ma/jobs/pfe-2")
    session.add_all([app1, app2])
    session.commit()

    c1 = get_or_create_company(session, app1)
    c2 = get_or_create_company(session, app2)

    assert c1.id == c2.id


def test_get_or_create_updates_website_if_provided() -> None:
    session = make_memory_session()
    app = make_application(job_url="https://adzuna.ma/jobs/1")  # would be skipped
    session.add(app)
    session.commit()

    company = get_or_create_company(session, app, website="https://techcorp.ma")

    assert company.website == "https://techcorp.ma"


# ---------------------------------------------------------------------------
# Unit tests — upsert_contact / upsert_email deduplication
# ---------------------------------------------------------------------------


def test_upsert_contact_deduplicates_by_linkedin_url() -> None:
    session = make_memory_session()
    app = make_application()
    session.add(app)
    session.commit()
    company = get_or_create_company(session, app)

    candidate = ContactCandidate(
        name="Alice Martin",
        job_title="Talent Acquisition Manager",
        linkedin_url="https://linkedin.com/in/alice-martin",
        professional_email=None,
        email_verification_status=None,
        email_confidence=None,
        source="public_website",
        source_url="https://techcorp.ma",
    )

    c1 = upsert_contact(session, company, candidate)
    c2 = upsert_contact(session, company, candidate)

    assert c1.id == c2.id


def test_upsert_contact_deduplicates_by_name_and_title() -> None:
    session = make_memory_session()
    app = make_application()
    session.add(app)
    session.commit()
    company = get_or_create_company(session, app)

    candidate = ContactCandidate(
        name="Bob Recruiter",
        job_title="HR Manager",
        linkedin_url=None,
        professional_email=None,
        email_verification_status=None,
        email_confidence=None,
        source="public_website",
        source_url=None,
    )

    c1 = upsert_contact(session, company, candidate)
    c2 = upsert_contact(session, company, candidate)

    assert c1.id == c2.id


def test_upsert_email_deduplicates_by_address() -> None:
    session = make_memory_session()
    app = make_application()
    session.add(app)
    session.commit()
    company = get_or_create_company(session, app)

    candidate = EmailCandidate(
        address="hr@techcorp.ma",
        kind=EmailKind.generic,
        provider="public_website",
        verification_status="unverified",
        confidence=0.5,
        source_url="https://techcorp.ma/contact",
    )

    e1 = upsert_email(session, company, candidate)
    e2 = upsert_email(session, company, candidate)

    assert e1.id == e2.id


def test_upsert_email_normalizes_address_to_lowercase() -> None:
    session = make_memory_session()
    app = make_application()
    session.add(app)
    session.commit()
    company = get_or_create_company(session, app)

    e = upsert_email(
        session,
        company,
        EmailCandidate(
            address="HR@TechCorp.MA",
            kind=EmailKind.generic,
            provider="public_website",
            verification_status=None,
            confidence=None,
            source_url=None,
        ),
    )

    assert e.address == "hr@techcorp.ma"


# ---------------------------------------------------------------------------
# Integration tests — research_application with mocked providers
# ---------------------------------------------------------------------------


def test_research_application_with_mocked_public_website_provider() -> None:
    """research_application should persist company + contacts + emails returned by provider."""
    session = make_memory_session()
    app = make_application()
    session.add(app)
    session.commit()

    mock_result = ProviderResearchResult(
        website="https://techcorp.ma",
        linkedin_url="https://linkedin.com/company/techcorp",
        description="A great tech company.",
        contacts=[
            ContactCandidate(
                name="Zineb Recruiter",
                job_title="Talent Acquisition",
                linkedin_url="https://linkedin.com/in/zineb",
                professional_email="zineb@techcorp.ma",
                email_verification_status="unverified",
                email_confidence=0.7,
                source="public_website",
                source_url="https://techcorp.ma/contact",
            )
        ],
        emails=[
            EmailCandidate(
                address="zineb@techcorp.ma",
                kind=EmailKind.unverified_individual,
                provider="public_website",
                verification_status="unverified",
                confidence=0.7,
                source_url="https://techcorp.ma/contact",
            )
        ],
    )

    from app.integrations.research_providers import PublicWebsiteProvider
    from app.services.research import research_application

    with patch.object(PublicWebsiteProvider, "research", new_callable=AsyncMock, return_value=mock_result):
        result = asyncio.run(
            research_application(
                session,
                app,
                ResearchRequest(providers=["public_website"]),
                Settings(),
            )
        )

    assert result.company.name == "TechCorp SARL"
    assert result.company.linkedin_url == "https://linkedin.com/company/techcorp"
    assert len(result.recruiters) == 1
    assert result.recruiters[0].name == "Zineb Recruiter"
    assert len(result.professional_emails) == 1
    assert result.professional_emails[0].address == "zineb@techcorp.ma"
    assert result.provider_errors == []


def test_research_application_links_application_to_company() -> None:
    """After research, application.company_id must be set."""
    session = make_memory_session()
    app = make_application()
    session.add(app)
    session.commit()

    from app.integrations.research_providers import PublicWebsiteProvider
    from app.services.research import research_application

    empty_result = ProviderResearchResult()

    with patch.object(PublicWebsiteProvider, "research", new_callable=AsyncMock, return_value=empty_result):
        result = asyncio.run(
            research_application(
                session,
                app,
                ResearchRequest(providers=["public_website"]),
                Settings(),
            )
        )

    assert result.company.id is not None
    session.refresh(app)
    assert app.company_id == result.company.id


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------


def test_research_endpoint_returns_404_for_unknown_opportunity() -> None:
    from app.main import app as fastapi_app

    client = TestClient(fastapi_app)
    response = client.post("/opportunities/99999/research", json={})
    assert response.status_code == 404
    assert "Application not found" in response.json()["detail"]


def test_companies_endpoint_returns_empty_list_initially() -> None:
    """GET /companies should return 200 even with no data."""
    from app.main import app as fastapi_app

    client = TestClient(fastapi_app)
    response = client.get("/companies")
    assert response.status_code == 200
    # result is a list (may have items from previous test runs on shared SQLite)
    assert isinstance(response.json(), list)


def test_company_not_found_returns_404() -> None:
    from app.main import app as fastapi_app

    client = TestClient(fastapi_app)
    response = client.get("/companies/99999")
    assert response.status_code == 404


def test_company_contacts_endpoint_returns_404_for_unknown_company() -> None:
    from app.main import app as fastapi_app

    client = TestClient(fastapi_app)
    response = client.get("/companies/99999/contacts")
    assert response.status_code == 404


def test_company_emails_endpoint_returns_404_for_unknown_company() -> None:
    from app.main import app as fastapi_app

    client = TestClient(fastapi_app)
    response = client.get("/companies/99999/emails")
    assert response.status_code == 404


def test_missing_hunter_key_returns_error_in_provider_errors() -> None:
    """Hunter without API key should produce a clear error in provider_errors, not an HTTP 500."""
    from app.services.research import research_application

    session = make_memory_session()
    app = make_application()
    session.add(app)
    session.commit()

    result = asyncio.run(
        research_application(
            session,
            app,
            ResearchRequest(providers=["hunter"], website="https://techcorp.ma"),
            Settings(hunter_api_key=None),
        )
    )

    assert any("HUNTER_API_KEY" in err for err in result.provider_errors)
