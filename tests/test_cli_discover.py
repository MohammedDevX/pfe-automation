"""Tests for CLI discovery label output and error handling aggregation."""

import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.cli_discover import run_cli_discovery, parse_args
from app.integrations.web_search import DuckDuckGoSearchEngine
from app.schemas import SearchCriteria


def test_cli_high_value_label_printed(capsys, monkeypatch):
    # Mock settings to avoid real DB connection
    dummy_settings = SimpleNamespace(
        database_url="postgresql://user:pass@localhost/db",
        dry_run_email=True,
        dry_run_ats=True,
        openai_api_key="dummy",
        hunter_api_key="dummy",
        web_search_api_key=None,
        web_search_engine="duckduckgo",
        web_search_max_queries_per_run=1,
        web_search_max_results_per_query=1,
        web_search_max_candidate_pages_per_run=1,
    )
    # Mock get_settings to return dummy settings
    monkeypatch.setattr("app.database.get_settings", lambda: dummy_settings)
    # Mock Base.metadata.create_all and ensure_additive_columns to no-op
    monkeypatch.setattr("app.database.Base.metadata.create_all", lambda bind: None)
    monkeypatch.setattr("app.main.ensure_additive_columns", lambda: None)
    # Prepare a dummy result object with the new label value
    dummy_result = SimpleNamespace(
        sources_queried=["duckduckgo"],
        jobs_fetched=0,
        jobs_normalized=0,
        new_opportunities=0,
        new_high_value_opportunities=0,
        duplicates_ignored=0,
        rejected_low_score=0,
        errors_by_provider={},
        applications=[],
        new_application_ids=[],
        morocco_count=0,
        france_count=0,
        remote_count=0,
    )
    # Patch the discovery function to return the dummy result
    with patch("app.services.discovery.search_and_persist", return_value=dummy_result):
        # Run CLI without JSON flag
        exit_code = run_cli_discovery(parse_args([]))
        captured = capsys.readouterr()
        assert exit_code == 0
        # Verify the new high‑value label is present
        assert "High-Value (Score >= 60, newly inserted)" in captured.out


def test_duckduckgo_non_200_error_recorded(monkeypatch):
    # Mock the HTTP post to return a non‑200 response
    class DummyResponse:
        def __init__(self, status_code):
            self.status_code = status_code
            self.text = ""
    async def dummy_post(*args, **kwargs):
        return DummyResponse(status_code=202)
    # Patch httpx.AsyncClient.post used inside DuckDuckGoSearchEngine.search
    monkeypatch.setattr("httpx.AsyncClient.post", dummy_post)
    # Build a minimal Settings object
    settings = SimpleNamespace(
        web_search_engine="duckduckgo",
        web_search_api_key=None,
        brave_search_api_key=None,
        web_search_max_queries_per_run=1,
        web_search_max_results_per_query=1,
        web_search_max_candidate_pages_per_run=1,
    )
    provider = DuckDuckGoSearchEngine()
    # Directly invoke the engine's search to trigger the RuntimeError
    with pytest.raises(RuntimeError) as excinfo:
        # Run inside an event loop
        import asyncio
        asyncio.run(provider.search("test"))
    assert "DuckDuckGo HTML endpoint returned 202" in str(excinfo.value)
