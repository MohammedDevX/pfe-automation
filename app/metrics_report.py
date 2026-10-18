import json
import sys
from datetime import timezone
from pathlib import Path
from typing import Optional

from app.models import DiscoveryRun, DiscoveryRunStatus
from app.schemas import ProviderStats


def _provider_stats_to_dict(stats: ProviderStats) -> dict:
    """Serialize ProviderStats preserving all fields defined in the model."""
    return {
        "provider_name": stats.provider_name,
        "countries_covered": stats.countries_covered,
        "access_method": stats.access_method,
        "status": stats.status,
        "jobs_fetched": stats.jobs_fetched,
        "jobs_normalized": stats.jobs_normalized,
        "new_jobs": stats.new_jobs,
        "duplicates": stats.duplicates,
        "low_score_jobs": stats.low_score_jobs,
        "errors_or_restrictions": stats.errors_or_restrictions,
    }


def write_metrics_report(
    run: DiscoveryRun, *, output_path: Optional[Path] = None
) -> Optional[Path]:
    """Generate a per‑run JSON metrics artifact.

    Parameters
    ----------
    run:
        Persisted ``DiscoveryRun`` ORM instance.
    output_path:
        Destination file. If ``None`` a path of the form
        ``tmp/metrics_<run_id>.json`` relative to the repository root is used.
        The function creates parent directories if needed.

    Returns
    -------
    pathlib.Path | None
        Absolute path of the written file, or ``None`` if serialization fails.
    """
    try:
        # Core scalar fields from the ORM
        core = {
            "run_timestamp": (
                (
                    run.started_at.astimezone(timezone.utc).replace(tzinfo=None)
                    if run.started_at.tzinfo
                    else run.started_at
                ).isoformat(timespec="microseconds")
                + "Z"
            ),
            "run_id": run.run_id,
            "status": run.status.value if isinstance(run.status, DiscoveryRunStatus) else str(run.status),
            "offers_fetched": run.jobs_fetched,
            "offers_normalized": run.jobs_normalized,
            "new_opportunities": run.new_opportunities,
            "new_high_value_opportunities": run.new_high_value_opportunities,
            "duplicates_ignored": run.duplicates_ignored,
            "rejected_low_score": run.rejected_low_score,
            "errors_by_provider": run.errors_json or {},
        }

        # Provider stats – persisted as JSON list of dicts
        raw_stats = run.provider_metrics_json or []
        provider_stats = [_provider_stats_to_dict(ProviderStats(**p)) for p in raw_stats]

        # Country counts are not stored on the ORM – emit nulls
        country_counts = {
            "morocco": None,
            "france": None,
            "canada": None,
            "belgium": None,
            "switzerland": None,
            "remote": None,
            "other": None,
        }

        # Missing‑credential detection – only the exact status string defined in ProviderStats
        missing_credentials = [p["provider_name"] for p in provider_stats if p["status"] == "credentials_missing"]
        provider_errors = {
            p["provider_name"]: p["errors_or_restrictions"]
            for p in provider_stats
            if p["errors_or_restrictions"]
        }

        report = {
            **core,
            "provider_stats": provider_stats,
            "country_counts": country_counts,
            "score_distribution": None,
            "technology_family_breakdown": None,
            "missing_credentials": missing_credentials,
            "provider_errors": provider_errors,
        }

        if output_path is None:
            out_dir = Path("tmp")
            out_dir.mkdir(parents=True, exist_ok=True)
            output_path = out_dir / f"metrics_{run.run_id}.json"

        output_path = Path(output_path).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8") as fp:
            json.dump(report, fp, indent=2, sort_keys=True, ensure_ascii=False)
        return output_path
    except Exception as exc:  # pragma: no cover – exercised by deterministic failure test
        print(f"⚠️  Failed to generate metrics report: {exc!r}", file=sys.stderr)
        return None
