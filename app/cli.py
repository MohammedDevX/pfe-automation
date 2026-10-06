"""Unified CLI entrypoint for PFE Job Automation tools.

Provides subcommands:
  - discover: Run opportunity discovery engine
  - migrate-db: Safely migrate SQLite data to target PostgreSQL database
  - sync-sequences: Synchronize PostgreSQL primary key sequences with MAX(id)
"""
import argparse
import sys


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="PFE Job Automation CLI Unified Tool",
        prog="python -m app.cli",
    )
    subparsers = parser.add_subparsers(dest="subcommand", help="Available subcommands")

    # Subcommand: discover
    p_discover = subparsers.add_parser("discover", help="Run opportunity discovery engine")
    p_discover.add_argument("--min-score", type=int, default=None, help="Minimum relevance score (0-100)")
    p_discover.add_argument("--providers", type=str, default=None, help="Comma-separated providers")
    p_discover.add_argument("--keywords", type=str, default=None, help="Comma-separated keywords")
    p_discover.add_argument("--locations", type=str, default=None, help="Comma-separated locations")
    p_discover.add_argument("--technologies", type=str, default=None, help="Comma-separated technologies")
    p_discover.add_argument("--json", action="store_true", help="Output summary as JSON")

    # Subcommand: migrate-db
    p_migrate = subparsers.add_parser("migrate-db", help="Migrate SQLite database to PostgreSQL/Neon")
    p_migrate.add_argument("--source-url", type=str, default="sqlite:///./pfe_jobs.db", help="Source SQLite database URL")
    p_migrate.add_argument("--target-url", type=str, default=None, help="Target PostgreSQL database URL")
    p_migrate.add_argument("--dry-run", action="store_true", help="Execute migration in dry-run mode (transaction rollback)")

    # Subcommand: sync-sequences
    p_sync = subparsers.add_parser("sync-sequences", help="Synchronize PostgreSQL primary key sequences with MAX(id)")
    p_sync.add_argument("--dry-run", action="store_true", help="Inspect sequence state without modifying DB")

    return parser.parse_args(args)


def run_cli(args: list[str] | None = None) -> int:
    parsed = parse_args(args)

    if parsed.subcommand == "discover":
        from app.cli_discover import run_cli_discovery
        return run_cli_discovery(parsed)
    elif parsed.subcommand == "migrate-db":
        import os
        from app.database import get_settings
        from app.migrate_sqlite_to_postgres import migrate_database

        target_url = parsed.target_url or get_settings().target_database_url or os.getenv("TARGET_DATABASE_URL")
        if not target_url:
            print("[ERROR] --target-url or TARGET_DATABASE_URL environment variable is required.", file=sys.stderr)
            return 1
        try:
            migrate_database(source_url=parsed.source_url, target_url=target_url, dry_run=parsed.dry_run)
            return 0
        except Exception:
            return 1
    elif parsed.subcommand == "sync-sequences":
        import os
        from sqlalchemy import create_engine
        from app.database import get_settings
        from app.migrate_sqlite_to_postgres import _normalize_postgres_url
        from app.sync_postgres_sequences import sync_postgres_sequences

        target_url = get_settings().target_database_url or os.getenv("TARGET_DATABASE_URL") or get_settings().database_url
        if not target_url or not ("postgresql" in target_url or "postgres" in target_url):
            print("[ERROR] TARGET_DATABASE_URL or DATABASE_URL must be a PostgreSQL connection string.", file=sys.stderr)
            return 1
        try:
            eff_url = _normalize_postgres_url(target_url)
            engine = create_engine(eff_url, pool_pre_ping=True)
            with engine.begin() as conn:
                sync_postgres_sequences(conn, dry_run=parsed.dry_run)
            return 0
        except Exception as exc:
            print(f"[ERROR] Sequence sync failed: {exc}", file=sys.stderr)
            return 1
    else:
        parse_args(["--help"])
        return 1


def main() -> None:
    sys.exit(run_cli())


if __name__ == "__main__":
    main()
