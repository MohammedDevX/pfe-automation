from unittest.mock import MagicMock, call
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Application, Company
from app.sync_postgres_sequences import SequenceSyncResult, sync_postgres_sequences


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def test_sync_sequences_sqlite_graceful_fallback(db_session):
    """On SQLite, sync_postgres_sequences returns un-synchronized results gracefully without error."""
    res = sync_postgres_sequences(db_session)
    assert len(res) == 8
    for item in res:
        assert item.synchronized is False
        assert item.sequence_name is None


def test_sync_sequences_mocked_postgres():
    """Mock PostgreSQL connection to verify exact SQL setval commands for non-empty & empty tables."""
    mock_conn = MagicMock()
    mock_conn.dialect.name = "postgresql"

    # Setup mock return values for sequence query calls
    # For each table (companies, contacts, professional_emails, applications, application_events, outbound_messages, incoming_responses, discovery_runs)
    # Return max_id, seq_name, and seq_info
    def mock_execute(stmt, *args, **kwargs):
        sql = str(stmt).strip()
        mock_res = MagicMock()

        if "COALESCE(MAX(id)" in sql:
            if "companies" in sql:
                mock_res.scalar.return_value = 4
            elif "applications" in sql:
                mock_res.scalar.return_value = 86
            elif "application_events" in sql:
                mock_res.scalar.return_value = 138
            elif "outbound_messages" in sql:
                mock_res.scalar.return_value = 13
            elif "discovery_runs" in sql:
                mock_res.scalar.return_value = 4
            else:
                mock_res.scalar.return_value = 0
            return mock_res

        if "pg_get_serial_sequence" in sql:
            tbl = sql.split("'")[1]
            mock_res.scalar.return_value = f"public.{tbl}_id_seq"
            return mock_res

        if "SELECT last_value, is_called FROM" in sql:
            if "applications_id_seq" in sql:
                mock_res.fetchone.return_value = (86, True)
            elif "companies_id_seq" in sql:
                mock_res.fetchone.return_value = (4, True)
            elif "contacts_id_seq" in sql:
                mock_res.fetchone.return_value = (1, False)
            else:
                mock_res.fetchone.return_value = (1, True)
            return mock_res

        return mock_res

    mock_conn.execute.side_effect = mock_execute

    results = sync_postgres_sequences(mock_conn, dry_run=False)

    assert len(results) == 8

    # Verify setval calls made
    executed_sqls = [call[0][0].text for call in mock_conn.execute.call_args_list if hasattr(call[0][0], "text")]
    
    # Non-empty table with MAX(id)=86 -> setval('public.applications_id_seq', 86, true)
    assert any("setval('public.applications_id_seq', 86, true)" in s for s in executed_sqls)
    # Non-empty table with MAX(id)=4 -> setval('public.companies_id_seq', 4, true)
    assert any("setval('public.companies_id_seq', 4, true)" in s for s in executed_sqls)
    # Empty table with MAX(id)=0 -> setval('public.contacts_id_seq', 1, false)
    assert any("setval('public.contacts_id_seq', 1, false)" in s for s in executed_sqls)


def test_sync_sequences_preserves_existing_data_and_ids(db_session):
    """Verify that calling sequence sync utility does not modify row data or existing IDs."""
    c1 = Company(id=1, name="Company 1", normalized_name="company 1")
    c2 = Company(id=4, name="Company 4", normalized_name="company 4")
    a1 = Application(id=86, company="Co", position="Pos", source="test", job_url="http://test/86")
    db_session.add_all([c1, c2, a1])
    db_session.commit()

    sync_postgres_sequences(db_session)

    # Check data untouched
    companies = db_session.query(Company).all()
    assert len(companies) == 2
    assert {c.id for c in companies} == {1, 4}

    apps = db_session.query(Application).all()
    assert len(apps) == 1
    assert apps[0].id == 86


def test_cli_subcommands_parse():
    """Verify app.cli argument parser for discover, migrate-db, and sync-sequences subcommands."""
    from app.cli import parse_args

    args_disc = parse_args(["discover", "--min-score", "65"])
    assert args_disc.subcommand == "discover"
    assert args_disc.min_score == 65

    args_mig = parse_args(["migrate-db", "--dry-run"])
    assert args_mig.subcommand == "migrate-db"
    assert args_mig.dry_run is True

    args_sync = parse_args(["sync-sequences", "--dry-run"])
    assert args_sync.subcommand == "sync-sequences"
    assert args_sync.dry_run is True

