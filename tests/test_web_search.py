"""Tests for Advanced Web Search Discovery Strategy Engine."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, Settings
from app.integrations.web_search import (
    DuckDuckGoSearchEngine,
    JobPostingExtractor,
    MockSearchEngine,
    PageTypeDetector,
    SearchResult,
    SearchResultFilter,
    WebSearchDiscoveryProvider,
    WebSearchQueryGenerator,
)
import app.models
from app.schemas import OpportunityIn, SearchCriteria
from app.services.discovery import search_and_persist


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture()
def settings():
    return Settings(
        web_search_engine="mock",
        web_search_max_queries_per_run=10,
        web_search_max_results_per_query=5,
        web_search_max_candidate_pages_per_run=10,
    )


# ===========================================================================
# 1. Query Generation & Prioritization Tests
# ===========================================================================

def test_query_generation_and_tiering():
    gen = WebSearchQueryGenerator()
    criteria = SearchCriteria(keywords=["python"])
    queries = gen.generate_queries(criteria, max_queries=15)

    assert len(queries) <= 15
    # Tier 1 should feature Morocco PFE queries
    assert any("Casablanca" in q or "Rabat" in q or "Maroc" in q for q in queries[:10])
    # Distinct queries
    assert len(queries) == len(set(queries))


def test_query_budget_enforcement():
    gen = WebSearchQueryGenerator()
    criteria = SearchCriteria()
    queries = gen.generate_queries(criteria, max_queries=5)
    assert len(queries) == 5


# ===========================================================================
# 2. URL Cleaning & Search Result Filtering Tests
# ===========================================================================

def test_url_cleaning_and_tracking_params():
    raw_url = "https://example.com/jobs/dev-123/?utm_source=google&utm_medium=cpc&ref=jobboard#apply"
    cleaned = SearchResultFilter.clean_url(raw_url)
    assert cleaned == "https://example.com/jobs/dev-123"


def test_search_result_filtering_rejection():
    res_blog = SearchResult(title="10 Tips for Interview", url="https://medium.com/blog/tips", snippet="Advice", engine="mock")
    res_salary = SearchResult(title="Software Salary in US", url="https://glassdoor.com/Salaries/dev", snippet="Salary info", engine="mock")
    res_job = SearchResult(title="Stage PFE Développeur .NET", url="https://company.com/careers/stage-pfe-net", snippet="Stage PFE", engine="mock")

    assert SearchResultFilter.is_relevant_search_result(res_blog) is False
    assert SearchResultFilter.is_relevant_search_result(res_salary) is False
    assert SearchResultFilter.is_relevant_search_result(res_job) is True


# ===========================================================================
# 3. Page Type & ATS Detection Tests
# ===========================================================================

def test_page_type_detection_and_ats():
    gh_url = "https://boards.greenhouse.io/doctolib/jobs/123456"
    lever_url = "https://jobs.lever.co/blablacar/abc-def-123"
    smart_url = "https://jobs.smartrecruiters.com/company/789-job"

    ats_gh = PageTypeDetector.detect_ats(gh_url)
    assert ats_gh == {"ats": "greenhouse", "board": "doctolib", "external_id": "123456"}

    ats_lever = PageTypeDetector.detect_ats(lever_url)
    assert ats_lever == {"ats": "lever", "site": "blablacar", "external_id": "abc-def-123"}

    ats_smart = PageTypeDetector.detect_ats(smart_url)
    assert ats_smart == {"ats": "smartrecruiters"}


# ===========================================================================
# 4. JSON-LD & OpenGraph Extraction Tests
# ===========================================================================

def test_json_ld_extraction_single_object():
    html = """
    <html><head>
    <script type="application/ld+json">
    {
        "@context": "https://schema.org/",
        "@type": "JobPosting",
        "title": "Stage PFE Backend .NET",
        "hiringOrganization": { "@type": "Organization", "name": "Atlas Tech" },
        "jobLocation": { "address": { "@type": "PostalAddress", "addressLocality": "Casablanca", "addressCountry": "Morocco" } },
        "description": "<p>Stage PFE backend en C# / ASP.NET</p>",
        "datePosted": "2026-09-10T10:00:00Z"
    }
    </script>
    </head></html>
    """
    extracted = JobPostingExtractor.extract_from_html(html, "https://atlastech.ma/jobs/pfe")
    assert extracted is not None
    assert extracted["title"] == "Stage PFE Backend .NET"
    assert extracted["company"] == "Atlas Tech"
    assert extracted["location"] == "Casablanca, Morocco"
    assert extracted["extraction_method"] == "json_ld"


def test_json_ld_extraction_array_and_graph():
    html = """
    <html><head>
    <script type="application/ld+json">
    {
        "@context": "https://schema.org",
        "@graph": [
            { "@type": "WebPage", "name": "Careers Page" },
            {
                "@type": "JobPosting",
                "title": "Stage PFE Fullstack Angular",
                "hiringOrganization": { "name": "NextGen Systems" },
                "jobLocation": { "address": { "addressLocality": "Rabat" } },
                "description": "Fullstack Angular/Spring Boot"
            }
        ]
    }
    </script>
    </head></html>
    """
    extracted = JobPostingExtractor.extract_from_html(html, "https://nextgen.ma/careers/angular")
    assert extracted is not None
    assert extracted["title"] == "Stage PFE Fullstack Angular"
    assert extracted["company"] == "NextGen Systems"
    assert extracted["location"] == "Rabat"


def test_generic_url_with_valid_json_ld():
    html = """
    <html><head>
    <script type="application/ld+json">
    {
        "@type": "JobPosting",
        "title": "Software Engineer Intern PFE",
        "hiringOrganization": { "name": "CloudCo" }
    }
    </script>
    </head></html>
    """
    extracted = JobPostingExtractor.extract_from_html(html, "https://cloudco.io/p/12948")
    assert extracted is not None
    assert extracted["title"] == "Software Engineer Intern PFE"
    assert extracted["company"] == "CloudCo"


def test_opengraph_fallback_extraction():
    html = """
    <html><head>
    <meta property="og:title" content="Stage PFE DevOps - Dev Corp" />
    <meta property="og:description" content="Stage PFE Docker/Kubernetes" />
    <meta property="og:site_name" content="Dev Corp" />
    </head><body><h1>Stage PFE DevOps</h1></body></html>
    """
    extracted = JobPostingExtractor.extract_from_html(html, "https://devcorp.com/stage-devops")
    assert extracted is not None
    assert extracted["title"] == "Stage PFE DevOps - Dev Corp"
    assert extracted["company"] == "Dev Corp"
    assert extracted["extraction_method"] == "opengraph"


# ===========================================================================
# 5. Provider End-to-End & Deduplication Tests
# ===========================================================================

def test_missing_search_engine_credentials():
    st = Settings(web_search_engine="custom_api", web_search_api_key=None)
    provider = WebSearchDiscoveryProvider(st)
    assert provider.status == "credentials_missing"
    assert "No active search engine" in provider.restriction_reason


def test_web_search_provider_end_to_end_mocked():
    async def _run():
        mock_engine = MockSearchEngine(responses={
            "stage PFE": [
                SearchResult(
                    title="Stage PFE Développeur Java",
                    url="https://techfirm.ma/jobs/pfe-java",
                    snippet="Stage PFE Java Spring Boot",
                    engine="mock",
                    rank=1
                )
            ]
        })
        st = Settings(web_search_engine="mock")
        provider = WebSearchDiscoveryProvider(st, search_engine=mock_engine)

        mock_page_html = """
        <html><head>
        <script type="application/ld+json">
        {
            "@type": "JobPosting",
            "title": "Stage PFE Développeur Java",
            "hiringOrganization": { "name": "TechFirm" },
            "jobLocation": { "address": { "addressLocality": "Casablanca" } },
            "description": "Stage PFE Java / Spring Boot"
        }
        </script>
        </head></html>
        """

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "text/html; charset=utf-8"}
        mock_resp.text = mock_page_html

        with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=mock_resp)):
            criteria = SearchCriteria(providers=["web_search"], web_search_max_queries=1)
            results = await provider.search(criteria)

            assert len(results) == 1
            assert results[0].title == "Stage PFE Développeur Java"
            assert results[0].company == "TechFirm"
            assert results[0].source == "web_search:techfirm.ma"
            assert "discovery_method=web_search" in results[0].notes

    asyncio.run(_run())


def test_deduplication_against_existing_provider_pfedaba(db, settings):
    """Verifies that an offer discovered via Web Search that duplicates a PFE Daba offer is not re-saved."""
    async def _run():
        # Pre-populate DB with PFE Daba opportunity
        pfedaba_opp = OpportunityIn(
            source="pfedaba:stagiaires_ma",
            external_id="pfe-123",
            title="Stage PFE Fullstack .NET",
            company="Morocco Soft",
            url="https://ma.indeed.com/viewjob?jk=999",
            location="Casablanca, Morocco",
            description="Stage PFE .NET",
        )

        # First discover via pfedaba
        mock_pfedaba_resp = MagicMock()
        mock_pfedaba_resp.status_code = 200
        mock_pfedaba_resp.json.return_value = {
            "total": 1,
            "total_pages": 1,
            "data": [{
                "id": "pfe-123",
                "slug": "stage-pfe-fullstack-net",
                "title": "Stage PFE Fullstack .NET",
                "company_name": "Morocco Soft",
                "location": "Casablanca",
                "provider": "Stagiaires.ma",
                "link": "https://ma.indeed.com/viewjob?jk=999",
                "is_direct": False,
            }]
        }
        with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=mock_pfedaba_resp)):
            res1 = await search_and_persist(db, SearchCriteria(providers=["pfedaba"]), settings)
            assert res1.new_opportunities == 1

        # Now run web_search discovering the EXACT SAME offer
        mock_engine = MockSearchEngine(responses={
            "stage PFE": [
                SearchResult(
                    title="Stage PFE Fullstack .NET",
                    url="https://ma.indeed.com/viewjob?jk=999",
                    snippet="Stage PFE .NET",
                    engine="mock",
                )
            ]
        })

        mock_page_html = """
        <html><head>
        <script type="application/ld+json">
        {
            "@type": "JobPosting",
            "title": "Stage PFE Fullstack .NET",
            "hiringOrganization": { "name": "Morocco Soft" },
            "jobLocation": { "address": { "addressLocality": "Casablanca" } }
        }
        </script>
        </head></html>
        """
        mock_page_resp = MagicMock()
        mock_page_resp.status_code = 200
        mock_page_resp.headers = {"content-type": "text/html"}
        mock_page_resp.text = mock_page_html

        with patch.object(WebSearchDiscoveryProvider, "search", new=AsyncMock(return_value=[
            OpportunityIn(
                source="web_search:indeed.com",
                title="Stage PFE Fullstack .NET",
                company="Morocco Soft",
                url="https://ma.indeed.com/viewjob?jk=999",
                location="Casablanca, Morocco",
                description="Stage PFE .NET",
            )
        ])):
            res2 = await search_and_persist(db, SearchCriteria(providers=["web_search"]), settings)
            # Must be recognized as duplicate!
            assert res2.new_opportunities == 0
            assert res2.duplicates_ignored == 1

    asyncio.run(_run())
