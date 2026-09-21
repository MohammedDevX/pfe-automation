"""Tests for Phase 3.5.1 Company Research Engine.

Covers:
1. Research company from an opportunity.
2. Official website is preferred over aggregator source.
3. Existing company is reused (idempotency).
4. Re-running research does not create duplicates.
5. Missing website does not crash the request.
6. HTTP failure (403/404/timeout) is handled gracefully.
7. Unknown/unverified information is not fabricated.
8. Research provenance is preserved in metadata_json.
9. Application status transitions to researched upon successful research.
"""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base, Settings
from app.integrations.research_providers import (
    ProviderResearchResult,
    PublicWebsiteProvider,
    WebSearchCompanyProvider,
    extract_technology_signals,
)
from app.integrations.web_search import MockSearchEngine, SearchResult
from app.models import Application, ApplicationStatus, Company, Contact, EmailKind, ProfessionalEmail
from app.schemas import ResearchRequest
from app.services.research import (
    ContactCandidate,
    EmailCandidate,
    get_or_create_company,
    infer_company_website,
    normalize_company_name,
    research_application,
)


def make_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def make_app(
    company: str = "CloudScale SARL",
    position: str = "PFE .NET / C# Developer",
    job_url: str = "https://cloudscale.io/careers/pfe-net",
    location: str = "Casablanca, Morocco",
    description: str = "Stage PFE C# .NET Core, Angular, SQL Server, Docker, and AWS microservices.",
) -> Application:
    return Application(
        company=company,
        position=position,
        source="web_search",
        job_url=job_url,
        location=location,
        description=description,
        application_status=ApplicationStatus.discovered,
    )


# ---------------------------------------------------------------------------
# 1. Research company from opportunity (extracts tech signals, metadata, sources)
# ---------------------------------------------------------------------------

def test_research_company_from_opportunity_enriches_metadata_and_tech_signals():
    session = make_session()
    app = make_app()
    session.add(app)
    session.commit()

    mock_provider_result = ProviderResearchResult(
        website="https://cloudscale.io",
        careers_url="https://cloudscale.io/careers",
        linkedin_url="https://linkedin.com/company/cloudscale",
        description="CloudScale builds high-throughput cloud infrastructure.",
        technology_signals=["Python", "Kubernetes", "DevOps"],
        sources=[
            {"type": "official_website", "url": "https://cloudscale.io", "confidence": "high"},
            {"type": "careers_page", "url": "https://cloudscale.io/careers", "confidence": "high"},
        ],
    )

    with patch.object(PublicWebsiteProvider, "research", new_callable=AsyncMock, return_value=mock_provider_result):
        result = asyncio.run(
            research_application(
                session,
                app,
                ResearchRequest(providers=["public_website"]),
                Settings(),
            )
        )

    company = result.company
    assert company.name == "CloudScale SARL"
    assert company.normalized_name == "cloudscale"
    assert company.website == "https://cloudscale.io"
    assert company.linkedin_url == "https://linkedin.com/company/cloudscale"
    assert company.description == "CloudScale builds high-throughput cloud infrastructure."
    assert company.location == "Casablanca, Morocco"

    # Technology signals combined from description (.NET, C#, Angular, SQL, Docker, AWS) + website (Python, Kubernetes, DevOps)
    meta = company.metadata_json or {}
    signals = meta.get("technology_signals", [])
    assert ".NET" in signals
    assert "C#" in signals
    assert "Docker" in signals
    assert "AWS" in signals
    assert "Kubernetes" in signals

    # Provenance sources
    sources = meta.get("sources", [])
    assert any(s.get("type") == "job_source" for s in sources)
    assert any(s.get("type") == "official_website" for s in sources)
    assert any(s.get("type") == "careers_page" for s in sources)

    # Status transition to researched
    session.refresh(app)
    assert app.application_status == ApplicationStatus.researched


# ---------------------------------------------------------------------------
# 2. Official website is preferred over aggregator source
# ---------------------------------------------------------------------------

def test_infer_company_website_rejects_aggregators():
    # Direct company website -> inferred
    assert infer_company_website("https://datadog.com/jobs/123") == "https://datadog.com"
    assert infer_company_website("https://cloudscale.io/careers/pfe") == "https://cloudscale.io"

    # Aggregator / ATS domains -> not inferred
    assert infer_company_website("https://www.adzuna.ma/jobs/1") is None
    assert infer_company_website("https://jobs.lever.co/acme/123") is None
    assert infer_company_website("https://boards.greenhouse.io/acme/jobs/456") is None
    assert infer_company_website("https://www.linkedin.com/jobs/view/789") is None
    assert infer_company_website("https://www.welcometothejungle.com/fr/companies/acme/jobs/1") is None
    assert infer_company_website("https://www.stagiaires.ma/offres/1") is None
    assert infer_company_website("https://www.rekrute.com/offre/1") is None


# ---------------------------------------------------------------------------
# 3. Existing company is reused (idempotency)
# ---------------------------------------------------------------------------

def test_existing_company_reused_across_multiple_opportunities():
    session = make_session()
    app1 = make_app(company="DataTech SARL", job_url="https://datatech.ma/jobs/1")
    app2 = make_app(company="DataTech", job_url="https://datatech.ma/jobs/2")
    session.add_all([app1, app2])
    session.commit()

    c1 = get_or_create_company(session, app1)
    c2 = get_or_create_company(session, app2)

    assert c1.id == c2.id
    all_companies = list(session.scalars(select(Company)))
    assert len(all_companies) == 1


# ---------------------------------------------------------------------------
# 4. Re-running research does not create duplicates
# ---------------------------------------------------------------------------

def test_rerunning_research_is_strictly_idempotent():
    session = make_session()
    app = make_app()
    session.add(app)
    session.commit()

    mock_result = ProviderResearchResult(
        website="https://cloudscale.io",
        linkedin_url="https://linkedin.com/company/cloudscale",
        technology_signals=[".NET", "C#"],
        contacts=[
            ContactCandidate(
                name="Sara HR",
                job_title="Talent Acquisition",
                linkedin_url="https://linkedin.com/in/sara-hr",
                professional_email="sara@cloudscale.io",
                email_verification_status="unverified",
                email_confidence=0.8,
                source="public_website",
                source_url="https://cloudscale.io/team",
            )
        ],
        emails=[
            EmailCandidate(
                address="sara@cloudscale.io",
                kind=EmailKind.unverified_individual,
                provider="public_website",
                verification_status="unverified",
                confidence=0.8,
                source_url="https://cloudscale.io/team",
            )
        ],
    )

    with patch.object(PublicWebsiteProvider, "research", new_callable=AsyncMock, return_value=mock_result):
        # Run 1
        res1 = asyncio.run(
            research_application(
                session,
                app,
                ResearchRequest(providers=["public_website"]),
                Settings(),
            )
        )
        # Run 2
        res2 = asyncio.run(
            research_application(
                session,
                app,
                ResearchRequest(providers=["public_website"]),
                Settings(),
            )
        )

    assert res1.company.id == res2.company.id

    # Verify counts in DB
    assert len(list(session.scalars(select(Company)))) == 1
    assert len(list(session.scalars(select(Contact)))) == 1
    assert len(list(session.scalars(select(ProfessionalEmail)))) == 1


# ---------------------------------------------------------------------------
# 5. Missing website does not crash request
# ---------------------------------------------------------------------------

def test_missing_website_does_not_crash_request():
    session = make_session()
    app = make_app(job_url="https://boards.greenhouse.io/unknown/1")
    session.add(app)
    session.commit()

    empty_result = ProviderResearchResult(metadata={"public_website": "no company website available"})

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
    assert result.company.name == "CloudScale SARL"
    assert result.provider_errors == []


# ---------------------------------------------------------------------------
# 6. HTTP failure / 403 / 404 / timeouts handled gracefully
# ---------------------------------------------------------------------------

def test_http_failure_handled_gracefully_without_crashing():
    session = make_session()
    app = make_app()
    session.add(app)
    session.commit()

    # Provider raises an exception simulating network/HTTP failure
    with patch.object(PublicWebsiteProvider, "research", new_callable=AsyncMock, side_effect=RuntimeError("HTTP 403 Forbidden: Cloudflare challenge")):
        result = asyncio.run(
            research_application(
                session,
                app,
                ResearchRequest(providers=["public_website"]),
                Settings(),
            )
        )

    assert result.company.id is not None
    assert len(result.provider_errors) == 1
    assert "HTTP 403 Forbidden" in result.provider_errors[0]


# ---------------------------------------------------------------------------
# 7. Unknown information is not fabricated (null / empty when unverified)
# ---------------------------------------------------------------------------

def test_unverified_fields_are_not_fabricated():
    session = make_session()
    app = make_app(
        company="ObscureTech",
        job_url="https://boards.greenhouse.io/obscure/1",
        description="Junior developer role.",
    )
    session.add(app)
    session.commit()

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

    company = result.company
    meta = company.metadata_json or {}

    # Unverified fields must remain None / empty
    assert company.linkedin_url is None
    assert meta.get("industry") is None
    assert meta.get("careers_url") is None
    assert result.recruiters == []
    assert result.professional_emails == []


# ---------------------------------------------------------------------------
# 8. WebSearchCompanyProvider discovers website and LinkedIn without aggregators
# ---------------------------------------------------------------------------

def test_web_search_company_provider_extracts_clean_website_and_linkedin():
    session = make_session()
    app = make_app(company="AcmeRobotics", job_url="https://boards.greenhouse.io/acmerobotics/1")
    session.add(app)
    session.commit()
    company = get_or_create_company(session, app)

    mock_engine = MockSearchEngine(responses={
        'official website': [
            SearchResult(title="AcmeRobotics on Indeed", url="https://www.indeed.com/cmp/AcmeRobotics", snippet="", engine="mock"),
            SearchResult(title="AcmeRobotics Official Website", url="https://www.acmerobotics.com/about", snippet="", engine="mock"),
        ],
        'site:linkedin.com/company': [
            SearchResult(title="AcmeRobotics LinkedIn", url="https://www.linkedin.com/company/acmerobotics/", snippet="", engine="mock"),
        ],
    })

    provider = WebSearchCompanyProvider(Settings())
    with patch("app.integrations.web_search.build_search_engine", return_value=mock_engine):
        result = asyncio.run(provider.research(company, app))

    assert result.website == "https://www.acmerobotics.com"
    assert result.linkedin_url == "https://www.linkedin.com/company/acmerobotics"
    assert any(s.get("type") == "web_search" for s in result.sources)
    assert any(s.get("type") == "linkedin_company" for s in result.sources)


# ---------------------------------------------------------------------------
# 9. Technology signals extraction precision
# ---------------------------------------------------------------------------

def test_technology_signals_extraction_precision():
    text = "We are hiring a Software Engineer with expertise in C#, .NET Core, ASP.NET Core, Angular, Docker, Kubernetes, AWS, and PostgreSQL."
    signals = extract_technology_signals(text)

    assert ".NET" in signals
    assert "C#" in signals
    assert "ASP.NET Core" in signals
    assert "Angular" in signals
    assert "Docker" in signals
    assert "Kubernetes" in signals
    assert "AWS" in signals
    assert "PostgreSQL" in signals
    assert "Python" not in signals
    assert "Java" not in signals


# ---------------------------------------------------------------------------
# 10. Web search discovers website -> triggers PublicWebsiteProvider enrichment
# ---------------------------------------------------------------------------

def test_web_search_discovers_website_then_triggers_website_enrichment():
    """When opportunity has no website, WebSearch discovers it and triggers website enrichment."""
    session = make_session()
    # Opportunity from Greenhouse with no direct company website
    app = make_app(
        company="RoboScale",
        job_url="https://boards.greenhouse.io/roboscale/jobs/123",
        description="Looking for a backend developer.",
    )
    session.add(app)
    session.commit()

    mock_search_engine = MockSearchEngine(responses={
        'official website': [
            SearchResult(title="RoboScale Official Site", url="https://roboscale.com/home", snippet="", engine="mock"),
        ],
    })

    async def mock_public_research(comp, appl):
        if not comp.website:
            return ProviderResearchResult(metadata={"public_website": "no company website available"})
        return ProviderResearchResult(
            website=comp.website,
            careers_url=f"{comp.website}/careers",
            linkedin_url="https://linkedin.com/company/roboscale",
            description="RoboScale autonomous robotics solutions.",
            technology_signals=["ROS", "C++", "Python", "Docker"],
            sources=[
                {"type": "official_website", "url": comp.website, "confidence": "high"},
                {"type": "careers_page", "url": f"{comp.website}/careers", "confidence": "high"},
            ],
        )

    with patch("app.integrations.web_search.build_search_engine", return_value=mock_search_engine):
        with patch.object(PublicWebsiteProvider, "research", side_effect=mock_public_research):
            result = asyncio.run(
                research_application(
                    session,
                    app,
                    ResearchRequest(providers=["public_website", "web_search"]),
                    Settings(),
                )
            )

    company = result.company
    meta = company.metadata_json or {}

    # Discovered by web search and enriched by PublicWebsiteProvider
    assert company.website == "https://roboscale.com"
    assert company.linkedin_url == "https://linkedin.com/company/roboscale"
    assert company.description == "RoboScale autonomous robotics solutions."
    assert meta.get("careers_url") == "https://roboscale.com/careers"
    assert "Docker" in meta.get("technology_signals", [])
    assert "Python" in meta.get("technology_signals", [])

    # Provenance contains web_search + official_website + careers_page
    sources = meta.get("sources", [])
    assert any(s.get("type") == "web_search" for s in sources)
    assert any(s.get("type") == "official_website" for s in sources)
    assert any(s.get("type") == "careers_page" for s in sources)


# ---------------------------------------------------------------------------
# 11. Strict Idempotency Semantics across repeated runs
# ---------------------------------------------------------------------------

def test_strict_idempotency_semantics_across_repeated_research_runs():
    """Verify that repeated research runs preserve identical records without array/table duplication."""
    session = make_session()
    app = make_app(
        company="NexioCorp",
        job_url="https://nexio.ma/jobs/pfe",
        description="Backend C# developer.",
    )
    session.add(app)
    session.commit()

    mock_result = ProviderResearchResult(
        website="https://nexio.ma",
        careers_url="https://nexio.ma/careers",
        linkedin_url="https://linkedin.com/company/nexio",
        technology_signals=["C#", ".NET", "PostgreSQL"],
        sources=[
            {"type": "official_website", "url": "https://nexio.ma", "confidence": "high"},
            {"type": "careers_page", "url": "https://nexio.ma/careers", "confidence": "high"},
        ],
        contacts=[
            ContactCandidate(
                name="Karim HR",
                job_title="HR Director",
                linkedin_url="https://linkedin.com/in/karim-hr",
                professional_email="karim@nexio.ma",
                email_verification_status="valid",
                email_confidence=0.9,
                source="public_website",
                source_url="https://nexio.ma/team",
            )
        ],
        emails=[
            EmailCandidate(
                address="karim@nexio.ma",
                kind=EmailKind.verified_individual,
                provider="public_website",
                verification_status="valid",
                confidence=0.9,
                source_url="https://nexio.ma/team",
            )
        ],
    )

    with patch.object(PublicWebsiteProvider, "research", new_callable=AsyncMock, return_value=mock_result):
        # Run 1
        res1 = asyncio.run(
            research_application(
                session,
                app,
                ResearchRequest(providers=["public_website"]),
                Settings(),
            )
        )
        meta1 = dict(res1.company.metadata_json or {})
        tech_signals_1 = list(meta1.get("technology_signals", []))
        sources_1 = list(meta1.get("sources", []))

        # Run 2
        res2 = asyncio.run(
            research_application(
                session,
                app,
                ResearchRequest(providers=["public_website"]),
                Settings(),
            )
        )
        meta2 = dict(res2.company.metadata_json or {})
        tech_signals_2 = list(meta2.get("technology_signals", []))
        sources_2 = list(meta2.get("sources", []))

    # 1. Company ID is identical
    assert res1.company.id == res2.company.id

    # 2. Database row counts unchanged
    assert len(list(session.scalars(select(Company)))) == 1
    assert len(list(session.scalars(select(Contact)))) == 1
    assert len(list(session.scalars(select(ProfessionalEmail)))) == 1

    # 3. No duplicate technology signals
    assert tech_signals_1 == tech_signals_2
    assert len(tech_signals_2) == len(set(tech_signals_2))

    # 4. No duplicate source provenance items
    assert len(sources_1) == len(sources_2)
    source_tuples = [(s["type"], s["url"]) for s in sources_2]
    assert len(source_tuples) == len(set(source_tuples))

    # 5. Research timestamp is present and valid ISO string
    assert "research_timestamp" in meta2

