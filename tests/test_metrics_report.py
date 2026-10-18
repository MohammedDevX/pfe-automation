# tests/test_metrics_report.py
"""Unit tests for the :func:`app.metrics_report.write_metrics_report` helper.

The helper serialises a :class:`app.models.DiscoveryRun` ORM instance into a
JSON file and returns the absolute path of the written file (or ``None`` on
failure).  The tests cover normal operation, handling of missing data, correct
UTC timestamp handling, custom output locations, and the deterministic failure
scenario where ``Path.open`` raises ``OSError``.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

from app.metrics_report import write_metrics_report
from app.models import DiscoveryRun, DiscoveryRunStatus
from app.schemas import ProviderStats


def _make_run(
    *,
    run_id: str = "run_test",
    started_at: datetime = datetime(2023, 1, 1, 12, 0, 0),
    status: DiscoveryRunStatus = DiscoveryRunStatus.completed,
    jobs_fetched: int = 10,
    jobs_normalized: int = 9,
    new_opportunities: int = 5,
    new_high_value_opportunities: int = 2,
    duplicates_ignored: int = 1,
    rejected_low_score: int = 0,
    provider_metrics_json: List[Dict[str, Any]] | None = None,
    errors_json: Dict[str, Any] | None = None,
) -> DiscoveryRun:
    """Utility to construct a minimal ``DiscoveryRun`` instance for tests.

    The ORM class does not require a live DB session for attribute access –
    constructing the object directly is sufficient for the helper.
    """
    run = DiscoveryRun(
        run_id=run_id,
        started_at=started_at,
        status=status,
        jobs_fetched=jobs_fetched,
        jobs_normalized=jobs_normalized,
        new_opportunities=new_opportunities,
        new_high_value_opportunities=new_high_value_opportunities,
        duplicates_ignored=duplicates_ignored,
        rejected_low_score=rejected_low_score,
        provider_metrics_json=provider_metrics_json,
        errors_json=errors_json,
    )
    return run


def test_full_serialization(tmp_path: Path) -> None:
    # Build a provider entry with all fields populated
    provider = ProviderStats(
        provider_name="example",
        countries_covered=["FR", "MA"],
        access_method="API",
        status="active",
        jobs_fetched=100,
        jobs_normalized=95,
        new_jobs=20,
        duplicates=5,
        low_score_jobs=3,
        errors_or_restrictions=None,
    )
    run = _make_run(
        provider_metrics_json=[provider.model_dump()],
        errors_json={"example": "some error"},
    )
    out_path = write_metrics_report(run, output_path=tmp_path / "report.json")
    assert out_path is not None
    data = json.loads(out_path.read_text())
    # Core scalar fields
    assert data["run_id"] == run.run_id
    assert data["status"] == run.status.value
    assert data["offers_fetched"] == run.jobs_fetched
    assert data["offers_normalized"] == run.jobs_normalized
    # Timestamp ends with Z, is valid ISO-8601 without '+00:00Z', and parses correctly
    assert data["run_timestamp"].endswith("Z")
    assert "+00:00" not in data["run_timestamp"]
    parsed = datetime.fromisoformat(data["run_timestamp"].replace("Z", "+00:00"))
    assert parsed.tzinfo == timezone.utc
    # Provider stats list
    assert isinstance(data["provider_stats"], list)
    prov = data["provider_stats"][0]
    for field in (
        "provider_name",
        "countries_covered",
        "access_method",
        "status",
        "jobs_fetched",
        "jobs_normalized",
        "new_jobs",
        "duplicates",
        "low_score_jobs",
        "errors_or_restrictions",
    ):
        assert field in prov
    # Errors dict should be present
    assert data["errors_by_provider"] == {"example": "some error"}


def test_missing_provider_metrics(tmp_path: Path) -> None:
    run = _make_run(provider_metrics_json=None)
    out_path = write_metrics_report(run, output_path=tmp_path / "no_prov.json")
    data = json.loads(out_path.read_text())
    assert data["provider_stats"] == []


def test_missing_credentials_detection(tmp_path: Path) -> None:
    provider = ProviderStats(
        provider_name="cred_missing",
        countries_covered=[],
        access_method="API",
        status="credentials_missing",
        jobs_fetched=0,
        jobs_normalized=0,
        new_jobs=0,
        duplicates=0,
        low_score_jobs=0,
        errors_or_restrictions="auth failed",
    )
    run = _make_run(provider_metrics_json=[provider.model_dump()])
    out_path = write_metrics_report(run, output_path=tmp_path / "creds.json")
    data = json.loads(out_path.read_text())
    assert "cred_missing" in data["missing_credentials"]
    assert data["provider_errors"] == {"cred_missing": "auth failed"}


def test_timestamp_handling_naive_and_aware(tmp_path: Path) -> None:
    # 1. Naive datetime treated as UTC
    naive_dt = datetime(2023, 5, 1, 9, 30, 0, 123456)
    naive = _make_run(started_at=naive_dt)

    # 2. Timezone-aware UTC datetime
    aware_utc_dt = datetime(2023, 5, 1, 9, 30, 0, 123456, tzinfo=timezone.utc)
    aware_utc = _make_run(started_at=aware_utc_dt)

    # 3. Timezone-aware non-UTC datetime (e.g., UTC+2: 11:30:00+02:00 corresponds to 09:30:00 UTC)
    tz_plus_2 = timezone(timedelta(hours=2))
    aware_non_utc_dt = datetime(2023, 5, 1, 11, 30, 0, 123456, tzinfo=tz_plus_2)
    aware_non_utc = _make_run(started_at=aware_non_utc_dt)

    naive_path = write_metrics_report(naive, output_path=tmp_path / "naive.json")
    aware_utc_path = write_metrics_report(aware_utc, output_path=tmp_path / "aware_utc.json")
    aware_non_utc_path = write_metrics_report(aware_non_utc, output_path=tmp_path / "aware_non_utc.json")

    naive_ts = json.loads(naive_path.read_text())["run_timestamp"]
    aware_utc_ts = json.loads(aware_utc_path.read_text())["run_timestamp"]
    aware_non_utc_ts = json.loads(aware_non_utc_path.read_text())["run_timestamp"]

    # All representations represent the exact same UTC instant
    expected_iso = "2023-05-01T09:30:00.123456Z"
    assert naive_ts == expected_iso
    assert aware_utc_ts == expected_iso
    assert aware_non_utc_ts == expected_iso

    # Validate that none contain '+00:00Z' or any offset notation
    for ts in (naive_ts, aware_utc_ts, aware_non_utc_ts):
        assert ts.endswith("Z")
        assert "+00:00" not in ts
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        assert parsed == datetime(2023, 5, 1, 9, 30, 0, 123456, tzinfo=timezone.utc)


def test_custom_output_path(tmp_path: Path) -> None:
    run = _make_run()
    custom = tmp_path / "custom_dir" / "my_report.json"
    out_path = write_metrics_report(run, output_path=custom)
    assert out_path == custom.resolve()
    assert out_path.is_file()


def test_deterministic_failure(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    # Force OSError on the default output location
    run = _make_run()
    default_dir = Path("tmp")
    default_path = default_dir / f"metrics_{run.run_id}.json"
    # Ensure the directory exists for the test (the helper creates it anyway)
    default_path.parent.mkdir(parents=True, exist_ok=True)

    original_open = Path.open

    def fake_open(self, *args, **kwargs):  # type: ignore[override]
        if self.resolve() == default_path.resolve():
            raise OSError("forced failure for test")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fake_open, raising=False)
    result = write_metrics_report(run)  # uses the default path
    captured = capsys.readouterr()
    assert result is None
    assert "Failed to generate metrics report" in captured.err
