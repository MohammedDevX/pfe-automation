"""Tests for Advanced Web Search Discovery Strategy Engine."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, Settings
from app.integrations.web_search import (
    BraveSearchEngine,
    DuckDuckGoSearchEngine,
    JobPostingExtractor,
    MockSearchEngine,
    MultiSearchEngineAdapter,
    PageTypeDetector,
    SearchResult,
    SearchResultFilter,
    WebSearchDiscoveryProvider,
    WebSearchQueryGenerator,
    build_search_engine,
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


# ===========================================================================
# 6. Multi-Engine & Search Expansion Tests (Phase 3.7.4A)
# ===========================================================================

def test_multi_search_engine_adapter_success_merge_and_dedup():
    engine1 = MockSearchEngine({
        "test": [
            SearchResult(title="Job 1", url="https://example.com/job1?utm=x", snippet="S1", engine="m1"),
            SearchResult(title="Job 2", url="https://example.com/job2", snippet="S2", engine="m1"),
        ]
    })
    engine2 = MockSearchEngine({
        "test": [
            SearchResult(title="Job 1 Dup", url="https://example.com/job1", snippet="S1 dup", engine="m2"),
            SearchResult(title="Job 3", url="https://example.com/job3", snippet="S3", engine="m2"),
        ]
    })

    multi = MultiSearchEngineAdapter([engine1, engine2])

    async def _run():
        results = await multi.search("test", limit=10)
        # Should merge and deduplicate job1!
        urls = [r.url for r in results]
        assert len(results) == 3
        assert urls == [
            "https://example.com/job1?utm=x",
            "https://example.com/job2",
            "https://example.com/job3",
        ]

    asyncio.run(_run())


def test_multi_search_engine_adapter_one_engine_failure():
    failing_engine = MagicMock()
    failing_engine.search = AsyncMock(side_effect=RuntimeError("Search engine 1 down"))

    working_engine = MockSearchEngine({
        "test": [SearchResult(title="Job 1", url="https://example.com/job1", snippet="S1", engine="m2")]
    })

    multi = MultiSearchEngineAdapter([failing_engine, working_engine])

    async def _run():
        results = await multi.search("test", limit=10)
        assert len(results) == 1
        assert results[0].title == "Job 1"

    asyncio.run(_run())


def test_multi_search_engine_adapter_all_engines_failure():
    f1 = MagicMock()
    f1.search = AsyncMock(side_effect=RuntimeError("Engine 1 error"))
    f2 = MagicMock()
    f2.search = AsyncMock(side_effect=RuntimeError("Engine 2 error"))

    multi = MultiSearchEngineAdapter([f1, f2])

    async def _run():
        results = await multi.search("test", limit=10)
        assert results == []

    asyncio.run(_run())


def test_brave_search_engine_missing_key():
    brave = BraveSearchEngine(api_key=None)

    async def _run():
        res = await brave.search("test")
        assert res == []

    asyncio.run(_run())


def test_brave_search_engine_success_and_failure():
    async def _run():
        brave = BraveSearchEngine(api_key="mock_brave_key")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "web": {
                "results": [
                    {
                        "title": "Brave Job 1",
                        "url": "https://brave-test.com/job1",
                        "description": "Brave snippet",
                    }
                ]
            }
        }

        with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=mock_resp)):
            res = await brave.search("stage PFE", limit=5)
            assert len(res) == 1
            assert res[0].title == "Brave Job 1"
            assert res[0].engine == "brave"

        # Test failure handling
        fail_resp = AsyncMock(side_effect=RuntimeError("Brave API 500"))
        with patch("httpx.AsyncClient.get", new=fail_resp):
            with pytest.raises(RuntimeError):
                await brave.search("stage PFE", limit=5)

    asyncio.run(_run())


def test_query_generation_platform_queries():
    gen = WebSearchQueryGenerator()
    criteria = SearchCriteria()
    queries = gen.generate_queries(criteria, max_queries=50)

    # Check for platform queries
    assert any("site:linkedin.com/jobs/view" in q for q in queries)
    assert any("site:welcometothejungle.com" in q for q in queries)
    assert any("site:hellowork.com" in q for q in queries)
    assert any("site:jobs.smartrecruiters.com" in q for q in queries)
    assert any("site:jobs.ashbyhq.com" in q for q in queries)
    assert any("site:apply.workable.com" in q for q in queries)

    # Check query bounds and deduplication
    assert len(queries) <= 50
    assert len(queries) == len(set(queries))


def test_recruiter_search_multi_engine_and_query_expansion(db, settings):
    from app.models import Company, Application
    from app.integrations.research_providers import WebSearchRecruiterProvider

    async def _run():
        provider = WebSearchRecruiterProvider(settings)
        company = Company(name="Atlas Soft", website="https://atlassoft.ma")
        app = Application(company="Atlas Soft", position="Dev", job_url="https://atlassoft.ma/job")

        mock_engine = MockSearchEngine({
            "site:linkedin.com/in": [
                SearchResult(
                    title="Sarah Connor - Technical Recruiter - Atlas Soft",
                    url="https://www.linkedin.com/in/sarah-connor-recruiter",
                    snippet="Technical Recruiter at Atlas Soft. Email: sarah@atlassoft.ma",
                    engine="mock",
                )
            ]
        })

        with patch("app.integrations.web_search.build_search_engine", return_value=mock_engine):
            result = await provider.research(company, app)
            assert len(result.contacts) >= 1
            assert result.contacts[0].name == "Sarah Connor"
            assert result.contacts[0].job_title == "Technical Recruiter"

    asyncio.run(_run())


def test_discovery_score_floor_rejection_and_retention():
    # Test scoring levels & criteria floor
    from app.schemas import OpportunityIn
    from app.scoring import score_opportunity

    high_opp = OpportunityIn(
        source="test",
        title="Stage PFE Développeur Backend .NET",
        company="TechCo",
        url="http://test/high",
        description="Stage PFE .NET C# Casablanca",
    )
    med_opp = OpportunityIn(
        source="test",
        title="Junior Developer",
        company="DevCo",
        url="http://test/med",
        description="Junior developer role",
    )
    low_opp = OpportunityIn(
        source="test",
        title="Senior Director of Architecture",
        company="BigCo",
        url="http://test/low",
        description="10+ years experience required",
    )

    res_high = score_opportunity(high_opp)
    res_med = score_opportunity(med_opp)
    res_low = score_opportunity(low_opp)

    assert res_high.score >= 60
    assert 35 <= res_med.score < 60
    assert res_low.score < 35


def test_web_search_in_default_search_criteria_providers():
    c = SearchCriteria()
    assert "web_search" in c.providers


def test_explicit_provider_selection_without_web_search():
    from app.integrations.providers import build_providers
    c = SearchCriteria(providers=["greenhouse", "lever"])
    providers = build_providers(c, Settings())
    names = [p.name for p in providers]
    assert "greenhouse" in names
    assert "lever" in names
    assert "web_search" not in names


def test_all_providers_selection_includes_web_search():
    from app.integrations.providers import build_providers
    c = SearchCriteria(providers=["all"])
    providers = build_providers(c, Settings())
    names = [p.name for p in providers]
    assert "web_search" in names


def test_multi_search_engine_adapter_selection_with_api_keys():
    st = Settings(web_search_engine="duckduckgo", web_search_api_key="test-key-123")
    engine = build_search_engine(st)
    assert engine is not None
    assert type(engine).__name__ == "MultiSearchEngineAdapter"


def test_location_bonus_alone_cannot_qualify_non_technical_roles():
    from app.schemas import OpportunityIn
    from app.scoring import score_opportunity

    non_tech_roles = [
        "doctolib - Account Executive terrain - Metz (x/f/m)",
        "doctolib - Chargé(e) de comptes - Opticien / Audioprothésiste (x/f/m)",
        "doctolib - Executive Assistant C-level (x/f/m)",
        "doctolib - Kundenservicemitarbeiter (x/f/m)",
    ]
    for title in non_tech_roles:
        opp = OpportunityIn(
            source="greenhouse:doctolib",
            title=title,
            company="Doctolib",
            url="http://test/non-tech",
            location="Metz, Fes, Paris, France",
            description="Corporate role mentioning API, React dashboards, SQL reporting.",
        )
        res = score_opportunity(opp)
        assert res.score < 35, f"Expected non-technical role '{title}' to score < 35, got {res.score}"


def test_relevant_pfe_internships_pass_scoring_filter():
    from app.schemas import OpportunityIn
    from app.scoring import score_opportunity

    pfe_opp = OpportunityIn(
        source="arbeitnow",
        title="Software Engineer Intern - Deep Learning & Robotics (Stage PFE)",
        company="TechCorp",
        url="http://test/pfe",
        location="Paris, France",
        description="Stage PFE 6 mois Python, PyTorch, C++, Docker.",
    )
    res = score_opportunity(pfe_opp)
    assert res.score >= 75
    assert res.relevance_category in ("EXPLICIT_PFE", "EXPLICIT_INTERNSHIP")
