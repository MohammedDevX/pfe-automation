"""CLI entrypoint for running the PFE Job Automation discovery engine.

Executes the discovery engine directly from the command line without starting
the FastAPI server or the background scheduler.

Usage:
    python -m app.cli_discover
    python app/cli_discover.py --min-score 60
    python app/cli_discover.py --providers greenhouse,lever --keywords backend,.net
"""

import argparse
import asyncio
import logging
import sys
from datetime import datetime

from app.database import Base, SessionLocal, engine, get_settings
from app.main import ensure_additive_columns
from app.schemas import SearchCriteria
from app.services.discovery import search_and_persist

logger = logging.getLogger("app.cli_discover")


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run PFE Job Automation opportunity discovery engine."
    )
    parser.add_argument(
        "--min-score",
        type=int,
        default=None,
        help="Minimum relevance score threshold (0-100). Default uses SearchCriteria default (50).",
    )
    parser.add_argument(
        "--providers",
        type=str,
        default=None,
        help="Comma-separated list of providers to query (e.g. greenhouse,lever,remotive).",
    )
    parser.add_argument(
        "--keywords",
        type=str,
        default=None,
        help="Comma-separated keywords (e.g. PFE,backend,.NET).",
    )
    parser.add_argument(
        "--locations",
        type=str,
        default=None,
        help="Comma-separated locations (e.g. Casablanca,Rabat,Remote).",
    )
    parser.add_argument(
        "--technologies",
        type=str,
        default=None,
        help="Comma-separated technologies (e.g. .NET,C#,Angular,Spring).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output discovery summary result as JSON.",
    )
    return parser.parse_args(args)


def run_cli_discovery(args: argparse.Namespace | None = None) -> int:
    if args is None:
        args = parse_args()

    # 1. Load settings & log safety state
    settings = get_settings()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    print("==================================================")
    print("🚀 PFE Job Discovery CLI Entrypoint")
    print("==================================================")
    print(f"Timestamp:      {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Database URL:   {settings.database_url}")
    print(f"DRY_RUN_EMAIL:  {settings.dry_run_email}")
    print(f"DRY_RUN_ATS:    {settings.dry_run_ats}")
    print("==================================================")

    # 2. Build search criteria
    kwargs = {}
    if args.min_score is not None:
        kwargs["min_score"] = args.min_score
    if args.providers:
        kwargs["providers"] = [p.strip() for p in args.providers.split(",") if p.strip()]
    if args.keywords:
        kwargs["keywords"] = [k.strip() for k in args.keywords.split(",") if k.strip()]
    if args.locations:
        kwargs["locations"] = [loc.strip() for loc in args.locations.split(",") if loc.strip()]
    if args.technologies:
        kwargs["technologies"] = [tech.strip() for tech in args.technologies.split(",") if tech.strip()]

    criteria = SearchCriteria(**kwargs)

    # 3. Initialize DB schema
    Base.metadata.create_all(bind=engine)
    ensure_additive_columns()

    # 4. Execute existing discovery engine
    db = SessionLocal()
    try:
        print("\n🔍 Running discovery engine...")
        result = asyncio.run(search_and_persist(db, criteria, settings))

        # 5. Print summary
        if args.json:
            import json
            summary_dict = {
                "sources_queried": result.sources_queried,
                "jobs_fetched": result.jobs_fetched,
                "jobs_normalized": result.jobs_normalized,
                "new_opportunities": result.new_opportunities,
                "new_high_value_opportunities": result.new_high_value_opportunities,
                "duplicates_ignored": result.duplicates_ignored,
                "rejected_low_score": result.rejected_low_score,
                "errors_by_provider": result.errors_by_provider,
                "applications_saved": len(result.applications),
            }
            print(json.dumps(summary_dict, indent=2))
        else:
            print("\n📊 DISCOVERY SUMMARY REPORT")
            print("--------------------------------------------------")
            print(f"Sources Queried:       {len(result.sources_queried)} ({', '.join(result.sources_queried)})")
            print(f"Jobs Fetched:          {result.jobs_fetched}")
            print(f"Jobs Normalized:       {result.jobs_normalized}")
            print(f"New Opportunities:     {result.new_opportunities}")
            print(f"High-Value (PFE/Intern):{result.new_high_value_opportunities}")
            print(f"Duplicates Ignored:    {result.duplicates_ignored}")
            print(f"Low Score Rejected:    {result.rejected_low_score}")
            print(f"Morocco / France / Remote: {result.morocco_count} / {result.france_count} / {result.remote_count}")

            if result.errors_by_provider:
                print("\n⚠️ Provider Warnings / Errors:")
                for p_name, err in result.errors_by_provider.items():
                    print(f"  - {p_name}: {err}")

            if result.applications:
                print("\n🏆 Top Discovered Opportunities:")
                for i, app in enumerate(result.applications[:10], 1):
                    cat_badge = f"[{app.relevance_category}] " if app.relevance_category else ""
                    print(f"  {i}. {cat_badge}{app.company} - {app.position} (Score: {app.score})")
                    print(f"     URL: {app.job_url}")

        print("\n✅ Discovery run completed successfully.")
        return 0

    except Exception as exc:
        print(f"\n❌ Discovery run failed with error: {exc}", file=sys.stderr)
        logger.exception("CLI discovery execution error")
        return 1

    finally:
        db.close()


def main() -> None:
    sys.exit(run_cli_discovery())


if __name__ == "__main__":
    main()
