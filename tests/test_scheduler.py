import pytest
import asyncio
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi.testclient import TestClient

from app.database import get_settings, SessionLocal, engine, Base
from app.main import app, ensure_additive_columns
from app.models import DiscoveryRun, DiscoveryRunStatus
from app.schemas import SearchCriteria, OpportunityIn
from app.scoring import ScoreResult
from app.services.scheduler import DiscoverySchedulerManager, scheduler_manager
from app.services.opportunities import upsert_opportunity
from app.integrations.web_search import WebSearchQueryGenerator


@pytest.fixture(autouse=True)
def setup_db():
    Base.metadata.create_all(bind=engine)
    ensure_additive_columns()
    yield


def test_scheduler_disabled_by_default():
    from app.database import Settings
    default_settings = Settings(_env_file=None)
    assert default_settings.discovery_scheduler_enabled is False
    assert default_settings.discovery_run_on_startup is False
    assert default_settings.discovery_scheduler_interval_hours == 24
    assert default_settings.discovery_scheduler_max_runtime_minutes == 30


def test_scheduler_manager_start_stop():
    async def _run():
        mgr = DiscoverySchedulerManager()
        settings = get_settings()
        
        # Should not start if disabled
        settings_disabled = settings.model_copy(update={"discovery_scheduler_enabled": False})
        mgr.start(settings_disabled)
        assert mgr._is_running is False
        
        # Enable and test start / stop
        settings_enabled = settings.model_copy(update={"discovery_scheduler_enabled": True})
        mgr.start(settings_enabled)
        assert mgr._is_running is True
        mgr.stop()
        assert mgr._is_running is False

    asyncio.run(_run())


def test_trigger_manual_run_creates_run_record():
    async def _run():
        mgr = DiscoverySchedulerManager()
        
        mock_result = MagicMock()
        mock_result.jobs_fetched = 5
        mock_result.jobs_normalized = 5
        mock_result.new_opportunities = 3
        mock_result.duplicates_ignored = 2
        mock_result.rejected_low_score = 0
        mock_result.sources_queried = ["Greenhouse", "Lever"]
        mock_result.errors_by_provider = {}
        mock_result.provider_stats = []
        mock_result.applications = []

        with patch("app.services.scheduler.search_and_persist", new=AsyncMock(return_value=mock_result)):
            run_rec = await mgr.trigger_manual_run(trigger_source="TEST_MANUAL")
            
            assert run_rec is not None
            assert run_rec.run_id.startswith("run_")
            assert run_rec.status == DiscoveryRunStatus.completed
            assert run_rec.jobs_fetched == 5
            assert run_rec.new_opportunities == 3

    asyncio.run(_run())


def test_concurrency_lock_skips_overlapping_run():
    async def _run():
        mgr = DiscoverySchedulerManager()
        
        await mgr._lock.acquire()
        try:
            run_rec = await mgr.trigger_manual_run(trigger_source="OVERLAP_TEST")
            assert run_rec.status == DiscoveryRunStatus.skipped
            assert "already in progress" in run_rec.errors_json.get("error", "")
        finally:
            mgr._lock.release()

    asyncio.run(_run())


def test_provider_failure_isolation():
    async def _run():
        mgr = DiscoverySchedulerManager()
        
        mock_result = MagicMock()
        mock_result.jobs_fetched = 2
        mock_result.jobs_normalized = 2
        mock_result.new_opportunities = 1
        mock_result.duplicates_ignored = 1
        mock_result.rejected_low_score = 0
        mock_result.sources_queried = ["Greenhouse", "FailingProvider"]
        mock_result.errors_by_provider = {"FailingProvider": "Connection timeout"}
        
        pstat1 = MagicMock()
        pstat1.provider_name = "Greenhouse"
        pstat1.status = "ok"
        pstat1.jobs_fetched = 2
        pstat1.jobs_normalized = 2
        pstat1.new_jobs = 1
        pstat1.duplicates = 1
        pstat1.low_score_jobs = 0
        pstat1.errors_or_restrictions = None

        pstat2 = MagicMock()
        pstat2.provider_name = "FailingProvider"
        pstat2.status = "error"
        pstat2.jobs_fetched = 0
        pstat2.jobs_normalized = 0
        pstat2.new_jobs = 0
        pstat2.duplicates = 0
        pstat2.low_score_jobs = 0
        pstat2.errors_or_restrictions = "Connection timeout"

        mock_result.provider_stats = [pstat1, pstat2]
        mock_result.applications = []

        with patch("app.services.scheduler.search_and_persist", new=AsyncMock(return_value=mock_result)):
            run_rec = await mgr.trigger_manual_run(trigger_source="ISOLATION_TEST")
            
            assert run_rec.status == DiscoveryRunStatus.partial
            assert "FailingProvider" in run_rec.errors_json["errors"]

    asyncio.run(_run())


def test_rest_api_trigger_and_list_discovery_runs():
    client = TestClient(app)
    
    mock_result = MagicMock()
    mock_result.jobs_fetched = 1
    mock_result.jobs_normalized = 1
    mock_result.new_opportunities = 1
    mock_result.duplicates_ignored = 0
    mock_result.rejected_low_score = 0
    mock_result.sources_queried = ["TestProvider"]
    mock_result.errors_by_provider = {}
    mock_result.provider_stats = []
    mock_result.applications = []

    with patch("app.services.scheduler.search_and_persist", new=AsyncMock(return_value=mock_result)):
        # POST /discovery/run
        res = client.post("/discovery/run", json={})
        assert res.status_code == 200
        data = res.json()
        assert "run_id" in data
        assert data["status"] == "COMPLETED"
        run_id = data["run_id"]

        # GET /discovery/runs
        res_list = client.get("/discovery/runs")
        assert res_list.status_code == 200
        runs = res_list.json()
        assert len(runs) >= 1
        assert any(r["run_id"] == run_id for r in runs)

        # GET /discovery/runs/{run_id}
        res_detail = client.get(f"/discovery/runs/{run_id}")
        assert res_detail.status_code == 200
        detail = res_detail.json()
        assert detail["run_id"] == run_id


def test_multi_technology_query_expansion():
    criteria = SearchCriteria()
    generator = WebSearchQueryGenerator()
    queries = generator.generate_queries(criteria, max_queries=100)

    query_str = " ".join(queries).lower()
    
    # Ensure broad technology coverage
    required_techs = ["c#", ".net", "java", "spring", "php", "symfony", "angular", "react", "full stack", "backend", "devops", "qa"]
    for tech in required_techs:
        assert tech in query_str, f"Expected technology '{tech}' in query expansion"


def test_cross_provider_deduplication_provenance():
    import uuid
    unique_company = f"Unique Dedup Co {uuid.uuid4().hex[:8]}"
    unique_position = f"Unique PFE Engineer {uuid.uuid4().hex[:8]}"
    db = SessionLocal()
    try:
        opp1 = OpportunityIn(
            company=unique_company,
            title=unique_position,
            url=f"https://example.com/unique_{uuid.uuid4().hex[:8]}",
            source="pfe_daba",
            location="Casablanca",
            description="Stage PFE C# Angular",
        )
        score1 = ScoreResult(score=85, reason="Match", relevance_category="EXPLICIT_PFE")

        app1, created1 = upsert_opportunity(db, opp1, score1)
        db.commit()
        assert created1 is True
        assert app1.source == "pfe_daba"

        # Second discovery from another provider (e.g. web_search)
        opp2 = OpportunityIn(
            company=unique_company,
            title=unique_position,
            url=f"https://example.com/unique_{uuid.uuid4().hex[:8]}_alt",
            source="web_search:duckduckgo",
            location="Casablanca",
            description="Stage PFE C# Angular",
        )
        score2 = ScoreResult(score=85, reason="Match", relevance_category="EXPLICIT_PFE")

        app2, created2 = upsert_opportunity(db, opp2, score2)
        db.commit()
        assert created2 is False
        assert app2.id == app1.id
        assert "also_discovered_by=web_search:duckduckgo" in app2.notes
    finally:
        db.close()
