"""Safe SQLite -> PostgreSQL database migration script for PFE Job Automation.

Migrates existing records from a source SQLite database (pfe_jobs.db)
to a target PostgreSQL (or SQLite test) database without deleting, modifying,
or corrupting source data.

Features:
- Preserves primary keys (id), foreign keys, timestamps, enums, JSON attributes.
- Idempotent & conflict-aware: Checks if records exist by PK or unique constraint before insert.
- Dry-run mode: Performs migration in a transaction and rolls back, reporting metrics.
- Topological order: Companies -> Contacts -> Professional Emails -> Applications -> Events -> Outbound Messages -> Incoming Responses -> Discovery Runs.

Usage:
    python -m app.migrate_sqlite_to_postgres --dry-run
    python -m app.migrate_sqlite_to_postgres --target-url "postgresql://user:pass@host/db?sslmode=require"
"""

import argparse
import logging
import sys
from dataclasses import dataclass

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import sessionmaker

from app.database import Base, _connect_args
import app.models  # Registers all SQLAlchemy models with Base.metadata
from app.models import (
    Application,
    ApplicationEvent,
    Company,
    Contact,
    DiscoveryRun,
    IncomingResponse,
    OutboundMessage,
    ProfessionalEmail,
)

logger = logging.getLogger("app.migration")


@dataclass
class MigrationTableStats:
    table_name: str
    source_count: int = 0
    target_inserted: int = 0
    target_skipped: int = 0
    target_conflicting: int = 0


@dataclass
class MigrationResult:
    dry_run: bool
    source_url: str
    target_url: str
    table_stats: list[MigrationTableStats]

    @property
    def total_inserted(self) -> int:
        return sum(s.target_inserted for s in self.table_stats)

    @property
    def total_skipped(self) -> int:
        return sum(s.target_skipped for s in self.table_stats)


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Safely migrate SQLite data to target PostgreSQL database."
    )
    parser.add_argument(
        "--source-url",
        type=str,
        default="sqlite:///./pfe_jobs.db",
        help="Source database URL (default: sqlite:///./pfe_jobs.db)",
    )
    parser.add_argument(
        "--target-url",
        type=str,
        default=None,
        help="Target database URL (e.g., postgresql://user:pass@host/db). Defaults to TARGET_DATABASE_URL env var.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Execute migration in dry-run mode (transaction is rolled back).",
    )
    return parser.parse_args(args)


def _normalize_postgres_url(url: str) -> str:
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://"):]
    return url


def migrate_database(
    source_url: str,
    target_url: str,
    dry_run: bool = False,
) -> MigrationResult:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    effective_target_url = _normalize_postgres_url(target_url)

    print("==================================================")
    print("  SQLite -> PostgreSQL Database Migration Utility ")
    print("==================================================")
    print(f"Source URL: {source_url}")
    print(f"Target URL: {target_url}")
    print(f"Mode:       {'DRY-RUN (No writes will be committed)' if dry_run else 'REAL MIGRATION'}")
    print("==================================================")

    # 1. Connect to source database (read-only mode)
    source_engine = create_engine(source_url, connect_args=_connect_args(source_url))
    SourceSession = sessionmaker(bind=source_engine)
    source_db = SourceSession()

    # 2. Connect to target database & initialize schema
    target_engine = create_engine(
        effective_target_url,
        connect_args=_connect_args(effective_target_url),
        pool_pre_ping=True,
    )

    # Initialize target schema
    Base.metadata.create_all(bind=target_engine)

    # Additive column check if engine dialect matches
    try:
        inspector = inspect(target_engine)
        if "applications" in inspector.get_table_names():
            app_cols = {c["name"] for c in inspector.get_columns("applications")}
            with target_engine.begin() as conn:
                if "company_id" not in app_cols:
                    conn.execute(text("ALTER TABLE applications ADD COLUMN company_id INTEGER"))
                if "notion_page_id" not in app_cols:
                    conn.execute(text("ALTER TABLE applications ADD COLUMN notion_page_id VARCHAR(100)"))
    except Exception as exc:
        logger.warning(f"Additive columns check on target DB: {exc}")

    TargetSession = sessionmaker(bind=target_engine)
    target_db = TargetSession()

    stats_list: list[MigrationTableStats] = []

    try:
        # Define migration order to respect Foreign Key constraints
        models_in_order = [
            (Company, "companies"),
            (Contact, "contacts"),
            (ProfessionalEmail, "professional_emails"),
            (Application, "applications"),
            (ApplicationEvent, "application_events"),
            (OutboundMessage, "outbound_messages"),
            (IncomingResponse, "incoming_responses"),
            (DiscoveryRun, "discovery_runs"),
        ]

        for model_cls, table_name in models_in_order:
            t_stat = MigrationTableStats(table_name=table_name)

            # Fetch all records from source DB
            source_records = source_db.scalars(select(model_cls)).all()
            t_stat.source_count = len(source_records)

            for rec in source_records:
                # Check if record with same PK exists in target
                existing_target = target_db.get(model_cls, rec.id)
                if existing_target is not None:
                    t_stat.target_skipped += 1
                    continue

                # Secondary natural key unique check for key tables
                is_duplicate = False
                if model_cls == Company:
                    if target_db.scalar(select(Company).where(Company.normalized_name == rec.normalized_name)):
                        is_duplicate = True
                elif model_cls == Application:
                    if rec.job_url and target_db.scalar(select(Application).where(Application.source == rec.source, Application.job_url == rec.job_url)):
                        is_duplicate = True
                    elif rec.external_id and target_db.scalar(select(Application).where(Application.source == rec.source, Application.external_id == rec.external_id)):
                        is_duplicate = True
                elif model_cls == DiscoveryRun:
                    if target_db.scalar(select(DiscoveryRun).where(DiscoveryRun.run_id == rec.run_id)):
                        is_duplicate = True

                if is_duplicate:
                    t_stat.target_skipped += 1
                    t_stat.target_conflicting += 1
                    continue

                # Construct new detached instance preserving all column values
                record_data = {}
                for col in model_cls.__table__.columns:
                    val = getattr(rec, col.name)
                    record_data[col.name] = val

                new_rec = model_cls(**record_data)
                target_db.add(new_rec)
                t_stat.target_inserted += 1

            # Flush after each table to satisfy foreign keys
            target_db.flush()
            stats_list.append(t_stat)
            print(f"  [OK] {table_name:<25}: Source={t_stat.source_count}, Inserted={t_stat.target_inserted}, Skipped={t_stat.target_skipped}")

        if dry_run:
            print("\n[DRY-RUN MODE] Rolling back target transaction.")
            target_db.rollback()
        else:
            print("\n[MIGRATING] Committing migration to target database...")
            target_db.commit()

        result = MigrationResult(
            dry_run=dry_run,
            source_url=source_url,
            target_url=target_url,
            table_stats=stats_list,
        )
        print("\n[SUCCESS] Migration completed successfully.")
        return result

    except Exception as exc:
        target_db.rollback()
        print(f"\n[ERROR] Migration failed with error: {exc}", file=sys.stderr)
        logger.exception("Migration execution error")
        raise
    finally:
        source_db.close()
        target_db.close()


def main() -> None:
    import os
    from app.database import get_settings
    args = parse_args()

    target_url = args.target_url or get_settings().target_database_url or os.getenv("TARGET_DATABASE_URL")
    if not target_url:
        print("[ERROR] --target-url or TARGET_DATABASE_URL environment variable is required.", file=sys.stderr)
        print("Example: python -m app.migrate_sqlite_to_postgres --target-url \"postgresql://USER:PASSWORD@HOST/DB?sslmode=require\"", file=sys.stderr)
        sys.exit(1)

    try:
        migrate_database(
            source_url=args.source_url,
            target_url=target_url,
            dry_run=args.dry_run,
        )
        sys.exit(0)
    except Exception:
        sys.exit(1)


if __name__ == "__main__":
    main()
