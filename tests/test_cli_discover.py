from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.cli_discover import parse_args, run_cli_discovery
from app.database import Base, Settings
import app.models  # register all models
from app.schemas import DiscoveryResult


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


def test_parse_args_defaults() -> None:
    args = parse_args([])
    assert args.min_score is None
    assert args.providers is None
    assert args.keywords is None
    assert args.locations is None
    assert args.technologies is None
    assert args.json is False


def test_parse_args_custom_values() -> None:
    args = parse_args([
        "--min-score", "70",
        "--providers", "greenhouse,lever",
        "--keywords", "PFE,backend",
        "--locations", "Casablanca,Rabat",
        "--technologies", ".NET,C#",
        "--json",
    ])
    assert args.min_score == 70
    assert args.providers == "greenhouse,lever"
    assert args.keywords == "PFE,backend"
    assert args.locations == "Casablanca,Rabat"
    assert args.technologies == ".NET,C#"
    assert args.json is True


def test_run_cli_discovery_success(db, settings) -> None:
    """CLI discovery executes search_and_persist and returns 0."""
    fake_result = DiscoveryResult(
        sources_queried=["jobicy"],
        jobs_fetched=1,
        jobs_normalized=1,
        new_opportunities=1,
        new_high_value_opportunities=1,
        duplicates_ignored=0,
        rejected_low_score=0,
        errors_by_provider={},
        morocco_count=1,
        france_count=0,
        remote_count=0,
        provider_stats=[],
        applications=[],
    )

    with patch("app.cli_discover.search_and_persist", new_callable=AsyncMock) as mock_search, \
         patch("app.cli_discover.get_settings", return_value=settings):
        mock_search.return_value = fake_result

        args = parse_args(["--providers", "jobicy", "--min-score", "60"])
        exit_code = run_cli_discovery(args)

        assert exit_code == 0
        assert mock_search.called
        call_criteria = mock_search.call_args[0][1]
        assert call_criteria.min_score == 60
        assert call_criteria.providers == ["jobicy"]


def test_run_cli_discovery_json_output(db, settings, capsys) -> None:
    """CLI discovery outputs valid JSON when --json flag is passed."""
    fake_result = DiscoveryResult(
        sources_queried=["jobicy"],
        jobs_fetched=1,
        jobs_normalized=1,
        new_opportunities=1,
        new_high_value_opportunities=1,
        duplicates_ignored=0,
        rejected_low_score=0,
        errors_by_provider={},
        morocco_count=1,
        france_count=0,
        remote_count=0,
        provider_stats=[],
        applications=[],
    )

    with patch("app.cli_discover.search_and_persist", new_callable=AsyncMock) as mock_search, \
         patch("app.cli_discover.get_settings", return_value=settings):
        mock_search.return_value = fake_result

        args = parse_args(["--json"])
        exit_code = run_cli_discovery(args)

        assert exit_code == 0
        captured = capsys.readouterr()
        assert '"new_opportunities": 1' in captured.out
        assert '"sources_queried": [' in captured.out


def test_run_cli_discovery_failure_returns_non_zero(db, settings) -> None:
    """CLI discovery handles unhandled exceptions and returns status code 1."""
    with patch("app.cli_discover.search_and_persist", side_effect=RuntimeError("API Failure")), \
         patch("app.cli_discover.get_settings", return_value=settings):

        args = parse_args([])
        exit_code = run_cli_discovery(args)

        assert exit_code == 1
