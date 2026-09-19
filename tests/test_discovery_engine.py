"""Discovery engine tests — calls the service layer directly to avoid
the TestClient/lifespan engine conflict (same pattern as test_opportunities.py).
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, Settings
from app.schemas import OpportunityIn, SearchCriteria
from app.services.discovery import search_and_persist
import app.models  # register all models with Base.metadata


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture()
def settings():
    return Settings(adzuna_app_id=None, adzuna_app_key=None)


# ---------- multi-source aggregation ----------

def test_discovery_aggregation_and_filtering(db, settings):
    """Multi-source fetch, cross-fetch dedup, low-score rejection, geo stats."""
    adzuna_jobs = [
        OpportunityIn(
            source="adzuna", external_id="a1",
            title="Backend Intern PFE", company="Startup A",
            url="http://a1", location="Casablanca, Morocco",
            description="Python stage",
        ),
        OpportunityIn(
            source="adzuna", external_id="a2",
            title="Chief Architect", company="BigCorp",
            url="http://a2", location="Paris, France",
            description="Senior manager role",
        ),
    ]
    gh_jobs = [
        OpportunityIn(
            source="greenhouse:test", external_id="g1",
            title="Stage developpeur PFE", company="Startup B",
            url="http://g1", location="Remote",
            description="Remote stage",
        ),
        # Duplicate of a1 by company+title+location
        OpportunityIn(
            source="greenhouse:test", external_id="g2",
            title="Backend Intern PFE", company="Startup A",
            url="http://a1-dup", location="Casablanca, Morocco",
            description="Duplicate!",
        ),
    ]

    criteria = SearchCriteria(
        providers=["adzuna", "greenhouse"],
        greenhouse_boards=["test"],
        min_score=50,
    )

    with patch("app.integrations.providers.AdzunaProvider.search", new_callable=AsyncMock) as mock_adzuna, \
         patch("app.integrations.providers.fetch_greenhouse", new_callable=AsyncMock) as mock_gh:
        mock_adzuna.return_value = adzuna_jobs
        mock_gh.return_value = gh_jobs

        result = asyncio.run(
            search_and_persist(db, criteria, settings)
        )

    assert "adzuna" in result.sources_queried
    assert "greenhouse" in result.sources_queried
    assert result.jobs_fetched == 4
    assert result.new_opportunities == 2          # intern + stage
    assert result.duplicates_ignored >= 1         # cross-fetch dup
    assert result.rejected_low_score == 1         # Chief Architect
    assert result.morocco_count >= 1
    assert result.remote_count >= 1


# ---------- provider failure resilience ----------

def test_discovery_provider_failure_does_not_crash(db, settings):
    """If one provider raises, the others still succeed."""
    lever_jobs = [
        OpportunityIn(
            source="lever:test", external_id="l1",
            title="Intern PFE Backend", company="Company L",
            url="http://l1", location="France",
            description="Stage",
        ),
    ]

    criteria = SearchCriteria(
        providers=["greenhouse", "lever"],
        greenhouse_boards=["test"],
        lever_sites=["test"],
    )

    with patch("app.integrations.providers.fetch_greenhouse", new_callable=AsyncMock) as mock_gh, \
         patch("app.integrations.providers.fetch_lever", new_callable=AsyncMock) as mock_lever:
        mock_gh.side_effect = Exception("API rate limit exceeded")
        mock_lever.return_value = lever_jobs

        result = asyncio.run(
            search_and_persist(db, criteria, settings)
        )

    assert result.errors_by_provider["greenhouse"] == "API rate limit exceeded"
    assert result.jobs_fetched == 1
    assert result.new_opportunities == 1


# ---------- already-known job ----------

def test_discovery_already_known_job_not_counted_as_new(db, settings):
    """Running discovery twice with the same job → second run counts 0 new."""
    lever_jobs = [
        OpportunityIn(
            source="lever:test", external_id="l1",
            title="Intern PFE Backend", company="Company L",
            url="http://l1", location="France",
            description="Stage",
        ),
    ]

    criteria = SearchCriteria(
        providers=["lever"],
        lever_sites=["test"],
    )

    with patch("app.integrations.providers.fetch_lever", new_callable=AsyncMock) as mock_lever:
        mock_lever.return_value = lever_jobs

        r1 = asyncio.run(
            search_and_persist(db, criteria, settings)
        )
        assert r1.new_opportunities == 1

        r2 = asyncio.run(
            search_and_persist(db, criteria, settings)
        )
        assert r2.new_opportunities == 0
        assert r2.duplicates_ignored == 1


# ---------- same job on two sources ----------

def test_same_job_discovered_on_two_sources(db, settings):
    """Same company+title+location from two providers → counted once."""
    adzuna_jobs = [
        OpportunityIn(
            source="adzuna", external_id="a1",
            title="Stage PFE Backend", company="SharedCo",
            url="http://adzuna/a1", location="Paris, France",
            description="Python stage intern",
        ),
    ]
    lever_jobs = [
        OpportunityIn(
            source="lever:shared", external_id="l1",
            title="Stage PFE Backend", company="SharedCo",
            url="http://lever/l1", location="Paris, France",
            description="Python stage intern",
        ),
    ]

    criteria = SearchCriteria(
        providers=["adzuna", "lever"],
        lever_sites=["shared"],
    )

    with patch("app.integrations.providers.AdzunaProvider.search", new_callable=AsyncMock) as mock_adzuna, \
         patch("app.integrations.providers.fetch_lever", new_callable=AsyncMock) as mock_lever:
        mock_adzuna.return_value = adzuna_jobs
        mock_lever.return_value = lever_jobs

        result = asyncio.run(
            search_and_persist(db, criteria, settings)
        )

    # Cross-fetch dedup catches the second occurrence
    assert result.new_opportunities == 1
    assert result.duplicates_ignored >= 1


# ---------- low-score filtering ----------

def test_low_score_jobs_are_rejected(db, settings):
    """Jobs scoring below min_score are filtered out."""
    jobs = [
        OpportunityIn(
            source="lever:x", external_id="low1",
            title="Senior Architect", company="Corp",
            url="http://low1", location="Paris, France",
            description="10+ years experience required. Architect leadership.",
        ),
    ]

    criteria = SearchCriteria(
        providers=["lever"],
        lever_sites=["x"],
        min_score=50,
    )

    with patch("app.integrations.providers.fetch_lever", new_callable=AsyncMock) as mock_lever:
        mock_lever.return_value = jobs

        result = asyncio.run(
            search_and_persist(db, criteria, settings)
        )

    assert result.rejected_low_score == 1
    assert result.new_opportunities == 0


# ---------- morocco filtering ----------

def test_morocco_location_counted(db, settings):
    """Jobs with Morocco-related locations are counted in morocco_count."""
    jobs = [
        OpportunityIn(
            source="lever:ma", external_id="m1",
            title="Stage PFE Backend", company="MaCo",
            url="http://m1", location="Casablanca, Morocco",
            description="Python stage intern",
        ),
    ]

    criteria = SearchCriteria(providers=["lever"], lever_sites=["ma"])

    with patch("app.integrations.providers.fetch_lever", new_callable=AsyncMock) as mock:
        mock.return_value = jobs
        result = asyncio.run(
            search_and_persist(db, criteria, settings)
        )

    assert result.morocco_count == 1


# ---------- france filtering ----------

def test_france_location_counted(db, settings):
    """Jobs with France-related locations are counted in france_count."""
    jobs = [
        OpportunityIn(
            source="lever:fr", external_id="f1",
            title="Stage PFE Backend", company="FrCo",
            url="http://f1", location="Paris, France",
            description="Python stage intern",
        ),
    ]

    criteria = SearchCriteria(providers=["lever"], lever_sites=["fr"])

    with patch("app.integrations.providers.fetch_lever", new_callable=AsyncMock) as mock:
        mock.return_value = jobs
        result = asyncio.run(
            search_and_persist(db, criteria, settings)
        )

    assert result.france_count == 1


# ---------- remote filtering ----------

def test_remote_location_counted(db, settings):
    """Jobs with 'Remote' or 'home based' are counted in remote_count."""
    jobs = [
        OpportunityIn(
            source="lever:rem", external_id="r1",
            title="Stage PFE Backend", company="RemCo",
            url="http://r1", location="Remote",
            description="Python stage intern",
        ),
    ]

    criteria = SearchCriteria(providers=["lever"], lever_sites=["rem"])

    with patch("app.integrations.providers.fetch_lever", new_callable=AsyncMock) as mock:
        mock.return_value = jobs
        result = asyncio.run(
            search_and_persist(db, criteria, settings)
        )

    assert result.remote_count == 1


# ---------- discovery statistics completeness ----------

def test_discovery_result_has_all_required_fields(db, settings):
    """DiscoveryResult contains all required stat fields."""
    criteria = SearchCriteria(providers=[])

    result = asyncio.run(
        search_and_persist(db, criteria, settings)
    )

    assert result.sources_queried == []
    assert result.jobs_fetched == 0
    assert result.jobs_normalized == 0
    assert result.new_opportunities == 0
    assert result.duplicates_ignored == 0
    assert result.rejected_low_score == 0
    assert result.errors_by_provider == {}
    assert result.morocco_count == 0
    assert result.france_count == 0
    assert result.canada_count == 0
    assert result.belgium_count == 0
    assert result.switzerland_count == 0
    assert result.remote_count == 0
    assert result.other_countries_count == 0
    assert result.provider_stats == []
    assert result.applications == []


# ===========================================================================
# International & Multilingual Regression Tests
# ===========================================================================

def test_morocco_opportunity(db, settings):
    """Morocco PFE opportunity receives high ranking and increments morocco_count."""
    jobs = [
        OpportunityIn(
            source="lever:test", external_id="m-1",
            title="Stage PFE Développeur Backend .NET", company="TechCasa",
            url="http://jobs.test/m1", location="Casablanca, Morocco",
            description="Stage de fin d'études de 6 mois pour un ingénieur backend C# .NET.",
        )
    ]
    with patch("app.integrations.providers.fetch_lever", new_callable=AsyncMock) as mock:
        mock.return_value = jobs
        res = asyncio.run(search_and_persist(db, SearchCriteria(providers=["lever"], lever_sites=["test"]), settings))

    assert res.morocco_count == 1
    assert len(res.applications) == 1
    assert res.applications[0].score >= 80
    assert "casablanca" in res.applications[0].score_reason.lower()


def test_france_opportunity(db, settings):
    """France PFE opportunity receives high ranking and increments france_count."""
    jobs = [
        OpportunityIn(
            source="greenhouse:test", external_id="f-1",
            title="Stage Ingénieur Logiciel Backend", company="DoctoParis",
            url="http://jobs.test/f1", location="Paris, France",
            description="Stage PFE 6 mois en backend Python FastAPI et PostgreSQL.",
        )
    ]
    with patch("app.integrations.providers.fetch_greenhouse", new_callable=AsyncMock) as mock:
        mock.return_value = jobs
        res = asyncio.run(search_and_persist(db, SearchCriteria(providers=["greenhouse"], greenhouse_boards=["test"]), settings))

    assert res.france_count == 1
    assert len(res.applications) == 1
    assert res.applications[0].score >= 80


def test_canadian_english_opportunity(db, settings):
    """Canadian English internship is accepted, scored, and counted under canada_count."""
    jobs = [
        OpportunityIn(
            source="lever:test", external_id="ca-1",
            title="Software Engineering Intern", company="MapleTech",
            url="http://jobs.test/ca1", location="Montreal, Canada",
            description="We are looking for an energetic software engineering intern with Python and SQL skills.",
        )
    ]
    with patch("app.integrations.providers.fetch_lever", new_callable=AsyncMock) as mock:
        mock.return_value = jobs
        res = asyncio.run(search_and_persist(db, SearchCriteria(providers=["lever"], lever_sites=["test"]), settings))

    assert res.canada_count == 1
    assert len(res.applications) == 1
    assert res.applications[0].score >= 50


def test_belgian_french_opportunity(db, settings):
    """Belgian French stage is accepted, scored, and counted under belgium_count."""
    jobs = [
        OpportunityIn(
            source="lever:test", external_id="be-1",
            title="Stage Développeur Backend", company="BelgoDev",
            url="http://jobs.test/be1", location="Bruxelles, Belgium",
            description="Stage de 5 mois pour étudiant fin d'études en informatique avec Python et Docker.",
        )
    ]
    with patch("app.integrations.providers.fetch_lever", new_callable=AsyncMock) as mock:
        mock.return_value = jobs
        res = asyncio.run(search_and_persist(db, SearchCriteria(providers=["lever"], lever_sites=["test"]), settings))

    assert res.belgium_count == 1
    assert len(res.applications) == 1
    assert res.applications[0].score >= 60


def test_swiss_opportunity(db, settings):
    """Swiss opportunity is accepted, scored, and counted under switzerland_count."""
    jobs = [
        OpportunityIn(
            source="lever:test", external_id="ch-1",
            title="Backend Developer Intern", company="SwissSoft",
            url="http://jobs.test/ch1", location="Zurich, Switzerland",
            description="Internship opportunity for computer science students. Python, Docker, API.",
        )
    ]
    with patch("app.integrations.providers.fetch_lever", new_callable=AsyncMock) as mock:
        mock.return_value = jobs
        res = asyncio.run(search_and_persist(db, SearchCriteria(providers=["lever"], lever_sites=["test"]), settings))

    assert res.switzerland_count == 1
    assert len(res.applications) == 1
    assert res.applications[0].score >= 50


def test_international_remote_opportunity(db, settings):
    """International remote opportunity is accepted, scored, and counted under remote_count."""
    jobs = [
        OpportunityIn(
            source="remotive", external_id="rem-1",
            title="Backend Engineering Intern", company="GlobalRemote",
            url="http://jobs.test/rem1", location="Worldwide (Remote)",
            description="Fully remote internship for software engineers. Python, FastAPI, PostgreSQL, Git.",
        )
    ]
    with patch("app.integrations.providers.RemotiveProvider.search", new_callable=AsyncMock) as mock:
        mock.return_value = jobs
        res = asyncio.run(search_and_persist(db, SearchCriteria(providers=["remotive"]), settings))

    assert res.remote_count == 1
    assert len(res.applications) == 1
    assert res.applications[0].score >= 70


def test_other_foreign_opportunity(db, settings):
    """Opportunities in other countries (e.g. Germany) are counted under other_countries_count."""
    jobs = [
        OpportunityIn(
            source="arbeitnow", external_id="de-1",
            title="Software Developer Intern", company="BerlinTech",
            url="http://jobs.test/de1", location="Berlin, Germany",
            description="Internship for students in Berlin. Python, SQL, React.",
        )
    ]
    with patch("app.integrations.providers.ArbeitnowProvider.search", new_callable=AsyncMock) as mock:
        mock.return_value = jobs
        res = asyncio.run(search_and_persist(db, SearchCriteria(providers=["arbeitnow"]), settings))

    assert res.other_countries_count == 1
    assert len(res.applications) == 1


def test_english_language_opportunity_not_penalized(db, settings):
    """English language postings are accepted and scored on technical merits."""
    jobs = [
        OpportunityIn(
            source="jobicy", external_id="en-1",
            title="Backend Python Developer Intern", company="EuroTech",
            url="http://jobs.test/en1", location="London, UK",
            description="Join our team as an engineering intern. You will develop backend services with Python, SQL, and Docker.",
        )
    ]
    with patch("app.integrations.providers.JobicyProvider.search", new_callable=AsyncMock) as mock:
        mock.return_value = jobs
        res = asyncio.run(search_and_persist(db, SearchCriteria(providers=["jobicy"]), settings))

    assert len(res.applications) == 1
    assert res.applications[0].score >= 60


def test_senior_foreign_job_with_incidental_internship_keywords(db, settings):
    """A senior role in a foreign country mentioning 'intern' in description gets PFE bonus suppressed."""
    jobs = [
        OpportunityIn(
            source="arbeitnow", external_id="snr-1",
            title="Senior Backend Engineer", company="MunichCorp",
            url="http://jobs.test/snr1", location="Munich, Germany",
            description="Lead the team. We also mentor interns and students. 5+ years required.",
        )
    ]
    with patch("app.integrations.providers.ArbeitnowProvider.search", new_callable=AsyncMock) as mock:
        mock.return_value = jobs
        res = asyncio.run(search_and_persist(db, SearchCriteria(providers=["arbeitnow"], min_score=50), settings))

    # Low score should be rejected
    assert res.rejected_low_score == 1
    assert len(res.applications) == 0


def test_visa_work_authorization_warning(db, settings):
    """Explicit work authorization requirement flags a warning in score_reason and notes."""
    jobs = [
        OpportunityIn(
            source="jobicy", external_id="visa-1",
            title="Software Engineering Intern", company="USTech",
            url="http://jobs.test/visa1", location="Remote",
            description="Software intern position. Must have work authorization in the United States. No visa sponsorship provided.",
        )
    ]
    with patch("app.integrations.providers.JobicyProvider.search", new_callable=AsyncMock) as mock:
        mock.return_value = jobs
        res = asyncio.run(search_and_persist(db, SearchCriteria(providers=["jobicy"]), settings))

    assert len(res.applications) == 1
    app = res.applications[0]
    assert "Visa/Auth Warning" in app.score_reason or (app.notes and "Visa/Auth Warning" in app.notes)


def test_provider_stats_populated(db, settings):
    """DiscoveryResult.provider_stats contains classified provider metadata."""
    criteria = SearchCriteria(providers=["greenhouse", "remotive"])
    with patch("app.integrations.providers.fetch_greenhouse", new_callable=AsyncMock) as mock_gh, \
         patch("app.integrations.providers.RemotiveProvider.search", new_callable=AsyncMock) as mock_rem:
        mock_gh.return_value = []
        mock_rem.return_value = []
        res = asyncio.run(search_and_persist(db, criteria, settings))

    assert len(res.provider_stats) == 2
    gh_stat = next(s for s in res.provider_stats if s.provider_name == "greenhouse")
    assert gh_stat.access_method == "API / structured public access"
    assert gh_stat.status == "active"
    assert "France" in gh_stat.countries_covered


# ===========================================================================
# Provider Direct Unit & Edge Case Tests
# ===========================================================================

def test_stagiaires_ma_provider_success():
    """StagiairesMaProvider correctly parses Schema.org JobPosting JSON-LD from HTML."""
    from app.integrations.providers import StagiairesMaProvider

    sample_html = """
    <html><head>
    <script type="application/ld+json">
    {
      "@type": "JobPosting",
      "title": "Stage PFE Backend Developer",
      "url": "https://stagiaires.ma/stage-emploi-maroc/9999",
      "datePosted": "2026-09-10T10:00:00Z",
      "description": "Stage de fin d'études backend .NET et C# à Casablanca.",
      "hiringOrganization": {"name": "AtlasTech"},
      "jobLocation": {
        "address": {"addressLocality": "Casablanca", "addressCountry": "MA"}
      }
    }
    </script>
    </head><body></body></html>
    """

    class MockResponse:
        text = sample_html
        def raise_for_status(self): pass

    provider = StagiairesMaProvider()
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = MockResponse()
        res = asyncio.run(provider.search(SearchCriteria()))

    assert len(res) == 1
    opp = res[0]
    assert opp.source == "stagiaires_ma"
    assert opp.external_id == "9999"
    assert opp.title == "Stage PFE Backend Developer"
    assert opp.company == "AtlasTech"
    assert "Casablanca" in opp.location


def test_stagiaires_ma_provider_malformed_html():
    """StagiairesMaProvider handles malformed HTML or JSON gracefully without crashing."""
    from app.integrations.providers import StagiairesMaProvider

    sample_html = "<html><head><script type='application/ld+json'>{invalid json}</script></head></html>"

    class MockResponse:
        text = sample_html
        def raise_for_status(self): pass

    provider = StagiairesMaProvider()
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = MockResponse()
        res = asyncio.run(provider.search(SearchCriteria()))

    assert res == []


def test_weworkremotely_provider_success():
    """WeWorkRemotelyProvider correctly parses programming RSS feed."""
    from app.integrations.providers import WeWorkRemotelyProvider

    sample_rss = """<?xml version="1.0"?>
    <rss version="2.0">
      <channel>
        <item>
          <title>AcmeCorp: Backend Engineer Intern</title>
          <link>https://weworkremotely.com/remote-jobs/acmecorp-backend-engineer-intern</link>
          <pubDate>Tue, 18 Aug 2026 20:00:00 +0000</pubDate>
          <description>Full-time remote internship in Python and PostgreSQL.</description>
        </item>
      </channel>
    </rss>
    """

    class MockResponse:
        text = sample_rss
        def raise_for_status(self): pass

    provider = WeWorkRemotelyProvider()
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = MockResponse()
        res = asyncio.run(provider.search(SearchCriteria()))

    assert len(res) == 1
    opp = res[0]
    assert opp.source == "weworkremotely"
    assert opp.company == "AcmeCorp"
    assert opp.title == "Backend Engineer Intern"
    assert opp.location == "Remote"


def test_remoteok_provider_success():
    """RemoteOKProvider parses JSON API correctly."""
    from app.integrations.providers import RemoteOKProvider

    sample_json = [
        {"legal": "terms"},
        {
            "slug": "remote-dev-intern-123",
            "position": "Software Engineering Intern",
            "company": "TechInc",
            "url": "https://remoteok.com/remote-jobs/remote-dev-intern-123",
            "location": "Worldwide",
            "date": "2026-09-10T12:00:00+00:00",
            "description": "Python dev internship."
        }
    ]

    class MockResponse:
        def json(self): return sample_json
        def raise_for_status(self): pass

    provider = RemoteOKProvider()
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = MockResponse()
        res = asyncio.run(provider.search(SearchCriteria()))

    assert len(res) == 1
    opp = res[0]
    assert opp.source == "remoteok"
    assert opp.external_id == "remote-dev-intern-123"
    assert opp.title == "Software Engineering Intern"
    assert opp.company == "TechInc"


def test_restricted_provider_raises_cleanly():
    """Restricted providers (e.g. IndeedMorocco, Rekrute) report restriction notes cleanly."""
    from app.integrations.providers import IndeedMoroccoProvider, RekruteProvider

    ind = IndeedMoroccoProvider()
    assert ind.access_method == "blocked/restricted"
    assert ind.status == "blocked/restricted"

    rek = RekruteProvider()
    assert rek.access_method == "manual-only"
    assert rek.status == "manual-only"

    with pytest.raises(RuntimeError, match="Cloudflare anti-bot challenge"):
        asyncio.run(ind.search(SearchCriteria()))


def test_discovery_relevance_category_ranking(db, settings):
    """Verify that search_and_persist ranks PFE and internship roles at the top,
    above full-time and senior roles."""
    jobs = [
        OpportunityIn(
            source="lever:test", external_id="j1",
            title="Senior Software Engineer (Backend)", company="BigCorp",
            url="http://test/j1", location="Paris, France",
            description="10+ years experience. Java Python Docker",
        ),
        OpportunityIn(
            source="lever:test", external_id="j2",
            title="Stage PFE Développeur Backend .NET", company="PfeCo",
            url="http://test/j2", location="Casablanca, Morocco",
            description="Stage de fin d'études 6 mois C# ASP.NET",
        ),
        OpportunityIn(
            source="lever:test", external_id="j3",
            title="Software Engineering Intern", company="InternCo",
            url="http://test/j3", location="Remote",
            description="Internship 6 months Python FastAPI",
        ),
        OpportunityIn(
            source="lever:test", external_id="j4",
            title="Software Engineer II", company="MidCo",
            url="http://test/j4", location="Paris",
            description="Software dev role Python",
        ),
    ]

    criteria = SearchCriteria(providers=["lever"], lever_sites=["test"], min_score=0)

    with patch("app.integrations.providers.fetch_lever", new_callable=AsyncMock) as mock:
        mock.return_value = jobs
        result = asyncio.run(search_and_persist(db, criteria, settings))

    titles_in_order = [a.position for a in result.applications]
    assert titles_in_order[0] == "Stage PFE Développeur Backend .NET"
    assert titles_in_order[1] == "Software Engineering Intern"
    assert titles_in_order[2] == "Software Engineer II"
    assert titles_in_order[3] == "Senior Software Engineer (Backend)"

    assert result.applications[0].relevance_category == "EXPLICIT_PFE"
    assert result.applications[1].relevance_category == "EXPLICIT_INTERNSHIP"
    assert result.applications[2].relevance_category == "FULL_TIME"
    assert result.applications[3].relevance_category == "SENIOR"


def test_pfedaba_provider_normalization():
    from app.integrations.providers import PfeDabaProvider

    provider = PfeDabaProvider()
    item = {
        "id": "abc-123-id",
        "slug": "stage-pfe-dev-fullstack-casablanca",
        "title": "Stage PFE Developpement Fullstack",
        "company_name": "Tech Corp",
        "location": "Casablanca",
        "field_domain": "Informatique",
        "provider": "Stagiaires.ma",
        "provider_link": "https://www.stagiaires.ma",
        "link": "https://ma.indeed.com/viewjob?jk=12345",
        "is_direct": False,
        "posted_date": "2026-09-10T10:00:00",
        "work_mode": "Sur site",
        "duration_months": 6,
        "description": "<p>Super stage PFE fullstack</p>",
    }

    opp = provider._normalize(item)
    assert opp is not None
    assert opp.source == "pfedaba:stagiaires_ma"
    assert opp.title == "Stage PFE Developpement Fullstack"
    assert opp.company == "Tech Corp"
    assert str(opp.url) == "https://ma.indeed.com/viewjob?jk=12345"
    assert opp.location == "Casablanca, Morocco"
    assert "original_source=Stagiaires.ma" in opp.notes
    assert "original_source_url=https://www.stagiaires.ma" in opp.notes
    assert "domain=Informatique" in opp.notes


def test_pfedaba_provider_search_mocked():
    from app.integrations.providers import PfeDabaProvider

    async def _run():
        provider = PfeDabaProvider()
        mock_payload = {
            "total": 1,
            "total_pages": 1,
            "data": [
                {
                    "id": "stage-1",
                    "slug": "stage-pfe-python",
                    "title": "Stage PFE Python Backend",
                    "company_name": "AI Systems",
                    "location": "Rabat",
                    "field_domain": "Informatique",
                    "provider": "LinkedIn",
                    "provider_link": "https://www.linkedin.com/",
                    "link": "https://www.linkedin.com/jobs/view/999",
                    "is_direct": False,
                    "posted_date": "2026-09-12T00:00:00",
                }
            ]
        }

        from unittest.mock import MagicMock
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = mock_payload
        mock_response.raise_for_status = lambda: None

        with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=mock_response)):
            criteria = SearchCriteria(keywords=["python"], max_pages=1)
            results = await provider.search(criteria)

            assert len(results) == 1
            assert results[0].title == "Stage PFE Python Backend"
            assert results[0].source == "pfedaba:linkedin"

    asyncio.run(_run())






