"""PostgreSQL Sequence Synchronization Utility for PFE Job Automation.

Synchronizes primary-key sequence generators (`table_id_seq`) with `MAX(id)`
after explicit ID insertions (such as SQLite -> PostgreSQL database migrations).
"""

import argparse
import logging
import sys
from dataclasses import dataclass

from sqlalchemy import Engine, inspect, text
from sqlalchemy.orm import Session

logger = logging.getLogger("app.sync_sequences")



SEQUENCE_TABLES = [
    "companies",
    "contacts",
    "professional_emails",
    "applications",
    "application_events",
    "outbound_messages",
    "incoming_responses",
    "discovery_runs",
]


@dataclass
class SequenceSyncResult:
    table_name: str
    pk_column: str
    max_id: int
    sequence_name: str | None
    old_last_value: int | None
    old_is_called: bool | None
    new_last_value: int | None
    new_is_called: bool | None
    next_expected_val: int | None
    synchronized: bool


def sync_postgres_sequences(
    bind: Engine | Session,
    tables: list[str] | None = None,
    dry_run: bool = False,
) -> list[SequenceSyncResult]:
    """Synchronize PostgreSQL primary key sequences with MAX(id) for given tables.

    For tables with MAX(id) >= 1:
        setval(seq, MAX(id), true)  -> nextval will return MAX(id) + 1
    For empty tables (MAX(id) == 0 or NULL):
        setval(seq, 1, false)       -> nextval will return 1

    Returns a list of SequenceSyncResult dataclasses detailing pre- and post-sync state.
    """
    target_tables = tables or SEQUENCE_TABLES
    results: list[SequenceSyncResult] = []

    # Obtain raw connection / engine dialect
    if isinstance(bind, Session):
        conn = bind.connection()
        dialect_name = bind.bind.dialect.name if bind.bind else "sqlite"
    else:
        conn = bind
        dialect_name = bind.dialect.name

    if dialect_name != "postgresql":
        # Non-PostgreSQL dialect (e.g. SQLite for unit testing)
        for tbl in target_tables:
            results.append(
                SequenceSyncResult(
                    table_name=tbl,
                    pk_column="id",
                    max_id=0,
                    sequence_name=None,
                    old_last_value=None,
                    old_is_called=None,
                    new_last_value=None,
                    new_is_called=None,
                    next_expected_val=None,
                    synchronized=False,
                )
            )
        return results

    for tbl in target_tables:
        max_id = conn.execute(text(f"SELECT COALESCE(MAX(id), 0) FROM {tbl}")).scalar() or 0
        seq_name = conn.execute(text(f"SELECT pg_get_serial_sequence('{tbl}', 'id')")).scalar()

        if not seq_name:
            logger.warning(f"No PostgreSQL sequence generator found for table '{tbl}' column 'id'")
            results.append(
                SequenceSyncResult(
                    table_name=tbl,
                    pk_column="id",
                    max_id=max_id,
                    sequence_name=None,
                    old_last_value=None,
                    old_is_called=None,
                    new_last_value=None,
                    new_is_called=None,
                    next_expected_val=None,
                    synchronized=False,
                )
            )
            continue

        # Inspect pre-sync sequence state
        old_val, old_called = None, None
        try:
            seq_info = conn.execute(text(f"SELECT last_value, is_called FROM {seq_name}")).fetchone()
            if seq_info:
                old_val, old_called = seq_info[0], seq_info[1]
        except Exception:
            pass

        # Perform setval synchronization
        if not dry_run:
            if max_id > 0:
                conn.execute(text(f"SELECT setval('{seq_name}', {max_id}, true)"))
            else:
                conn.execute(text(f"SELECT setval('{seq_name}', 1, false)"))

        # Inspect post-sync sequence state
        new_val, new_called, next_val = None, None, None
        try:
            seq_info_after = conn.execute(text(f"SELECT last_value, is_called FROM {seq_name}")).fetchone()
            if seq_info_after:
                new_val, new_called = seq_info_after[0], seq_info_after[1]
                next_val = (new_val + 1) if new_called else new_val
        except Exception:
            pass

        results.append(
            SequenceSyncResult(
                table_name=tbl,
                pk_column="id",
                max_id=max_id,
                sequence_name=seq_name,
                old_last_value=old_val,
                old_is_called=old_called,
                new_last_value=new_val,
                new_is_called=new_called,
                next_expected_val=next_val,
                synchronized=not dry_run,
            )
        )

    return results


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Synchronize PostgreSQL primary key sequences with MAX(id)."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Inspect sequences and display proposed corrections without modifying sequence state.",
    )
    return parser.parse_args(args)


def main() -> None:
    import os
    from sqlalchemy import create_engine
    from app.database import get_settings
    from app.migrate_sqlite_to_postgres import _normalize_postgres_url

    cli_args = parse_args()

    target_url = get_settings().target_database_url or os.getenv("TARGET_DATABASE_URL") or get_settings().database_url
    if not target_url or not ("postgresql" in target_url or "postgres" in target_url):
        print("[ERROR] TARGET_DATABASE_URL or DATABASE_URL must be a PostgreSQL connection string.", file=sys.stderr)
        sys.exit(1)

    eff_url = _normalize_postgres_url(target_url)
    engine = create_engine(eff_url, pool_pre_ping=True)

    print("==================================================")
    print("  PostgreSQL Primary Key Sequence Synchronization ")
    print("==================================================")
    print(f"Mode: {'DRY-RUN (Inspect sequence alignment)' if cli_args.dry_run else 'REAL SYNCHRONIZATION'}")
    print("==================================================")

    with engine.begin() as conn:
        sync_results = sync_postgres_sequences(conn, dry_run=cli_args.dry_run)

        for res in sync_results:
            print(f"\nTable:                 {res.table_name}")
            print(f"Primary Key Column:    {res.pk_column}")
            print(f"MAX(id):               {res.max_id}")
            print(f"Sequence Name:         {res.sequence_name or 'N/A'}")
            print(f"Old Sequence State:    last_value={res.old_last_value}, is_called={res.old_is_called}")
            if not cli_args.dry_run:
                print(f"New Sequence State:    last_value={res.new_last_value}, is_called={res.new_is_called}")
                print(f"Next Generated Value:  {res.next_expected_val}")
                print(f"Status:                {'SYNCHRONIZED [OK]' if res.synchronized else 'SKIPPED'}")

    print("\n==================================================")
    print("Sequence audit and synchronization completed.")
    print("==================================================")


if __name__ == "__main__":
    main()
