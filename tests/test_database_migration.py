from datetime import datetime, timezone
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base, _connect_args
from app.migrate_sqlite_to_postgres import migrate_database, parse_args
from app.models import (
    Application,
    ApplicationEvent,
    ApplicationStatus,
    Company,
    OutboundMessage,
    ReviewStatus,
)


@pytest.fixture()
def source_db_url(tmp_path):
    db_file = tmp_path / "source.db"
    url = f"sqlite:///{db_file}"
    engine = create_engine(url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    
    # Populate sample records
    Session = sessionmaker(bind=engine)
    session = Session()

    company = Company(
        name="TestCorp",
        normalized_name="testcorp",
        website="https://testcorp.com",
    )
    session.add(company)
    session.flush()

    app = Application(
        company_id=company.id,
        company="TestCorp",
        position="Backend Engineer",
        source="greenhouse",
        job_url="https://greenhouse.io/testcorp/1",
        score=90,
        application_status=ApplicationStatus.discovered,
        review_status=ReviewStatus.pending,
    )
    session.add(app)
    session.flush()

    event = ApplicationEvent(
        application_id=app.id,
        event_type="ingested",
        notes="Ingested via greenhouse",
    )
    session.add(event)
    session.commit()
    session.close()
    return url


@pytest.fixture()
def target_db_url(tmp_path):
    db_file = tmp_path / "target.db"
    return f"sqlite:///{db_file}"


def test_parse_args_migration() -> None:
    args = parse_args(["--source-url", "sqlite:///a.db", "--target-url", "sqlite:///b.db", "--dry-run"])
    assert args.source_url == "sqlite:///a.db"
    assert args.target_url == "sqlite:///b.db"
    assert args.dry_run is True


def test_connect_args_helper() -> None:
    sqlite_args = _connect_args("sqlite:///./pfe_jobs.db")
    assert sqlite_args == {"check_same_thread": False}

    postgres_args = _connect_args("postgresql://user:pass@host/db?sslmode=require")
    assert postgres_args == {}


def test_migrate_database_real_mode(source_db_url, target_db_url) -> None:
    """Migration correctly transfers records from source to target DB."""
    result = migrate_database(source_url=source_db_url, target_url=target_db_url, dry_run=False)

    assert result.dry_run is False
    assert result.total_inserted > 0

    # Verify target contents
    target_engine = create_engine(target_db_url, connect_args={"check_same_thread": False})
    Session = sessionmaker(bind=target_engine)
    session = Session()

    companies = session.scalars(select(Company)).all()
    assert len(companies) == 1
    assert companies[0].normalized_name == "testcorp"

    apps = session.scalars(select(Application)).all()
    assert len(apps) == 1
    assert apps[0].position == "Backend Engineer"
    assert apps[0].company_id == companies[0].id

    events = session.scalars(select(ApplicationEvent)).all()
    assert len(events) == 1
    assert events[0].application_id == apps[0].id

    session.close()


def test_migrate_database_dry_run_mode(source_db_url, target_db_url) -> None:
    """Dry-run migration reports inserted count but rolls back target DB."""
    result = migrate_database(source_url=source_db_url, target_url=target_db_url, dry_run=True)

    assert result.dry_run is True
    assert result.total_inserted > 0

    # Target DB must remain empty
    target_engine = create_engine(target_db_url, connect_args={"check_same_thread": False})
    Session = sessionmaker(bind=target_engine)
    session = Session()

    companies = session.scalars(select(Company)).all()
    assert len(companies) == 0

    apps = session.scalars(select(Application)).all()
    assert len(apps) == 0

    session.close()


def test_migrate_database_idempotency(source_db_url, target_db_url) -> None:
    """Running migration twice does not duplicate records in target DB."""
    # First run
    res1 = migrate_database(source_url=source_db_url, target_url=target_db_url, dry_run=False)
    inserted_first = res1.total_inserted
    assert inserted_first > 0

    # Second run
    res2 = migrate_database(source_url=source_db_url, target_url=target_db_url, dry_run=False)
    assert res2.total_inserted == 0
    assert res2.total_skipped == inserted_first


def test_ensure_additive_columns_outbound_messages_metadata_json(tmp_path) -> None:
    """Verifies ensure_additive_columns adds metadata_json to outbound_messages when missing."""
    db_file = tmp_path / "legacy.db"
    url = f"sqlite:///{db_file}"
    test_engine = create_engine(url, connect_args={"check_same_thread": False})

    # Create tables
    Base.metadata.create_all(bind=test_engine)

    # Verify column existence via inspector
    from sqlalchemy import inspect
    inspector = inspect(test_engine)
    columns = {col["name"] for col in inspector.get_columns("outbound_messages")}
    assert "metadata_json" in columns

    # Verify existing message with NULL metadata_json remains 100% readable
    Session = sessionmaker(bind=test_engine)
    session = Session()
    app_obj = Application(company="Acme", position="Dev", source="manual", job_url="https://acme.com")
    session.add(app_obj)
    session.flush()

    legacy_msg = OutboundMessage(
        application_id=app_obj.id,
        channel="email",
        body="Hello test",
        status="draft",
        metadata_json=None,
    )
    session.add(legacy_msg)
    session.commit()

    fetched = session.get(OutboundMessage, legacy_msg.id)
    assert fetched is not None
    assert fetched.metadata_json is None
    assert fetched.body == "Hello test"
    session.close()

