import asyncio
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, Settings
from app.integrations.research_providers import WebSearchRecruiterProvider, _parse_recruiter_search_item
from app.integrations.web_search import MockSearchEngine, SearchResult
from app.main import app as fastapi_app
from app.models import Application, ApplicationStatus, Company, Contact, ProfessionalEmail
from app.schemas import ContactOut, ResearchRequest
from app.services.research import (
    ContactCandidate,
    classify_contact_relevance,
    get_or_create_company,
    research_application,
    upsert_contact,
)


def make_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def make_application() -> Application:
    return Application(
        company="Acme Software",
        position="Backend Engineering Intern PFE",
        source="manual",
        job_url="https://acme.example.com/careers/pfe",
        location="Paris, France",
    )


def test_classify_contact_relevance_high():
    for title in [
        "Talent Acquisition Partner",
        "Technical Recruiter",
        "Campus Recruiting Manager",
        "HR Business Partner",
        "Head of Talent",
    ]:
        rel, reason = classify_contact_relevance(title)
        assert rel == "HIGH"
        assert "Explicit recruiting/HR role" in reason


def test_classify_contact_relevance_medium():
    for title in [
        "Engineering Manager",
        "Lead Developer",
        "Chief Technology Officer",
        "Head of Engineering",
        "VP Engineering",
    ]:
        rel, reason = classify_contact_relevance(title)
        assert rel == "MEDIUM"
        assert "Engineering leadership/hiring manager role" in reason


def test_classify_contact_relevance_low():
    for title in [
        "Software Engineer",
        "Product Manager",
        "Data Analyst",
        "Sales Representative",
        None,
    ]:
        rel, reason = classify_contact_relevance(title)
        assert rel == "LOW"


def test_web_search_recruiter_provider_parses_linkedin_title():
    item = SearchResult(
        title="Jane Doe - Senior Talent Acquisition Manager - Acme Software | LinkedIn",
        url="https://fr.linkedin.com/in/jane-doe-12345",
        snippet="Jane Doe is Senior Talent Acquisition Manager at Acme Software.",
        engine="mock",
        rank=1,
    )
    contacts, emails = _parse_recruiter_search_item(item, "Acme Software")

    assert len(contacts) == 1
    assert contacts[0].name == "Jane Doe"
    assert contacts[0].job_title == "Senior Talent Acquisition Manager"
    assert contacts[0].linkedin_url == "https://www.linkedin.com/in/jane-doe-12345"


def test_web_search_recruiter_provider_extracts_clean_linkedin_url():
    item = SearchResult(
        title="John Smith - Engineering Manager - Acme | LinkedIn",
        url="https://uk.linkedin.com/in/johnsmith99/?originalSubdomain=uk",
        snippet="Engineering Manager at Acme.",
        engine="mock",
        rank=1,
    )
    contacts, _ = _parse_recruiter_search_item(item, "Acme")

    assert len(contacts) == 1
    assert contacts[0].linkedin_url == "https://www.linkedin.com/in/johnsmith99"


def test_web_search_recruiter_provider_extracts_snippet_email():
    item = SearchResult(
        title="Sarah Connor - Technical Recruiter - Acme | LinkedIn",
        url="https://www.linkedin.com/in/sarah-connor",
        snippet="Contact Sarah at sarah.connor@acme.com for PFE applications.",
        engine="mock",
        rank=1,
    )
    contacts, emails = _parse_recruiter_search_item(item, "Acme", domain="acme.com")

    assert len(emails) == 1
    assert emails[0].address == "sarah.connor@acme.com"
    assert contacts[0].professional_email == "sarah.connor@acme.com"


def test_web_search_recruiter_provider_bounded_queries():
    session = make_session()
    app_obj = make_application()
    session.add(app_obj)
    session.commit()
    company = get_or_create_company(session, app_obj)

    mock_engine = MockSearchEngine({
        "talent acquisition": [
            SearchResult(
                title="Jane Doe - Recruiter - Acme Software | LinkedIn",
                url="https://www.linkedin.com/in/jane-doe",
                snippet="Recruiter at Acme.",
                engine="mock",
                rank=1,
            )
        ]
    })

    provider = WebSearchRecruiterProvider(Settings())
    with patch("app.integrations.web_search.build_search_engine", return_value=mock_engine):
        res = asyncio.run(provider.research(company, app_obj))

    assert len(res.contacts) >= 1
    assert res.contacts[0].name == "Jane Doe"


def test_web_search_recruiter_provider_handles_search_errors_gracefully():
    session = make_session()
    app_obj = make_application()
    session.add(app_obj)
    session.commit()
    company = get_or_create_company(session, app_obj)

    mock_engine = AsyncMock()
    mock_engine.search.side_effect = RuntimeError("Search quota exceeded")

    provider = WebSearchRecruiterProvider(Settings())
    with patch("app.integrations.web_search.build_search_engine", return_value=mock_engine):
        res = asyncio.run(provider.research(company, app_obj))

    assert res.contacts == []
    assert any("search_error" in k for k in res.metadata.keys())


def test_contact_deduplication_by_name_and_title():
    session = make_session()
    app_obj = make_application()
    session.add(app_obj)
    session.commit()
    company = get_or_create_company(session, app_obj)

    cand1 = ContactCandidate(
        name="Jane Doe",
        job_title="Talent Acquisition Specialist",
        linkedin_url="https://www.linkedin.com/in/janedoe",
        professional_email=None,
        email_verification_status=None,
        email_confidence=None,
        source="web_search_recruiter",
        source_url="https://www.linkedin.com/in/janedoe",
    )
    cand2 = ContactCandidate(
        name="Jane Doe",
        job_title="Talent Acquisition Specialist",
        linkedin_url="https://www.linkedin.com/in/janedoe",
        professional_email="jane@acme.com",
        email_verification_status="unverified",
        email_confidence=0.5,
        source="web_search_recruiter",
        source_url="https://www.linkedin.com/in/janedoe",
    )

    c1 = upsert_contact(session, company, cand1)
    c2 = upsert_contact(session, company, cand2)

    assert c1.id == c2.id
    contacts_in_db = session.query(Contact).filter_by(company_id=company.id).all()
    assert len(contacts_in_db) == 1


def test_contact_deduplication_across_research_runs():
    session = make_session()
    app_obj = make_application()
    session.add(app_obj)
    session.commit()

    mock_engine = MockSearchEngine({
        "talent acquisition": [
            SearchResult(
                title="Jane Doe - Recruiter - Acme Software | LinkedIn",
                url="https://www.linkedin.com/in/jane-doe",
                snippet="Recruiter at Acme Software.",
                engine="mock",
                rank=1,
            )
        ]
    })

    with patch("app.integrations.web_search.build_search_engine", return_value=mock_engine):
        req = ResearchRequest(providers=["web_search_recruiter"])
        r1 = asyncio.run(research_application(session, app_obj, req, Settings()))
        r2 = asyncio.run(research_application(session, app_obj, req, Settings()))

    contacts_in_db = session.query(Contact).all()
    assert len(contacts_in_db) == 1
    assert len(r1.recruiters) == 1
    assert len(r2.recruiters) == 1


def test_email_no_fabrication_strictly_verified_or_snippet():
    item = SearchResult(
        title="Bob Builder - Construction Manager - Acme | LinkedIn",
        url="https://www.linkedin.com/in/bob-builder",
        snippet="Bob builds software systems at Acme.",
        engine="mock",
        rank=1,
    )
    contacts, emails = _parse_recruiter_search_item(item, "Acme")

    assert len(emails) == 0
    assert contacts[0].professional_email is None


def test_contact_out_schema_populates_relevance():
    c_out = ContactOut(
        id=1,
        company_id=10,
        name="Jane Doe",
        job_title="Talent Acquisition Lead",
        linkedin_url="https://www.linkedin.com/in/jane-doe",
        professional_email="jane@acme.com",
        email_verification_status="unverified",
        email_confidence=0.5,
        source="web_search_recruiter",
        source_url="https://www.linkedin.com/in/jane-doe",
        discovered_at="2026-09-24T12:00:00",
    )

    assert c_out.relevance == "HIGH"
    assert "Explicit recruiting/HR role" in c_out.relevance_reason


def test_research_application_with_recruiter_provider():
    session = make_session()
    app_obj = make_application()
    session.add(app_obj)
    session.commit()

    mock_engine = MockSearchEngine({
        "talent acquisition": [
            SearchResult(
                title="Alice Smith - Technical Recruiter - Acme Software | LinkedIn",
                url="https://www.linkedin.com/in/alice-smith",
                snippet="Technical Recruiter at Acme Software.",
                engine="mock",
                rank=1,
            )
        ]
    })

    with patch("app.integrations.web_search.build_search_engine", return_value=mock_engine):
        req = ResearchRequest(providers=["public_website", "web_search", "web_search_recruiter"])
        result = asyncio.run(research_application(session, app_obj, req, Settings()))

    assert app_obj.application_status == ApplicationStatus.researched
    assert len(result.recruiters) >= 1
    assert result.recruiters[0].name == "Alice Smith"


def test_research_api_endpoint_returns_recruiters_with_relevance():
    session = make_session()
    app_obj = make_application()
    session.add(app_obj)
    session.commit()

    company = get_or_create_company(session, app_obj)
    c = Contact(
        company_id=company.id,
        name="Mark Lead",
        job_title="Engineering Manager",
        linkedin_url="https://www.linkedin.com/in/mark-lead",
        source="web_search_recruiter",
    )
    session.add(c)
    session.commit()

    def get_db_override():
        try:
            yield session
        finally:
            pass

    fastapi_app.dependency_overrides[__import__("app.main", fromlist=["get_db"]).get_db] = get_db_override
    client = TestClient(fastapi_app)

    try:
        response = client.get(f"/companies/{company.id}/contacts")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["name"] == "Mark Lead"
        assert data[0]["relevance"] == "MEDIUM"
        assert "Engineering leadership/hiring manager role" in data[0]["relevance_reason"]
    finally:
        fastapi_app.dependency_overrides.clear()


def test_generic_and_non_person_search_results_are_rejected():
    generic_items = [
        SearchResult(
            title="Talent Acquisition Team - Acme | LinkedIn",
            url="https://www.linkedin.com/company/acme",
            snippet="Meet the talent acquisition team at Acme.",
            engine="mock",
            rank=1,
        ),
        SearchResult(
            title="Recruiter Jobs in Paris - Indeed",
            url="https://www.indeed.com/jobs?q=recruiter",
            snippet="Find recruiter jobs in Paris.",
            engine="mock",
            rank=2,
        ),
        SearchResult(
            title="Hiring Managers Department - Acme",
            url="https://example.com/hiring-managers",
            snippet="Hiring manager contact directory.",
            engine="mock",
            rank=3,
        ),
        SearchResult(
            title="Working at Acme - Careers",
            url="https://example.com/careers",
            snippet="Learn about working at Acme.",
            engine="mock",
            rank=4,
        ),
        SearchResult(
            title="Top 10 Recruiters in Tech | Medium",
            url="https://medium.com/top-recruiters",
            snippet="Article listing top tech recruiters.",
            engine="mock",
            rank=5,
        ),
    ]

    for item in generic_items:
        contacts, _ = _parse_recruiter_search_item(item, "Acme")
        assert len(contacts) == 0, f"Generic result '{item.title}' should not produce a Contact"

