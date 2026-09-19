"""Regression tests for manual application 'J'ai envoyé / Mark as Applied' workflow.

Covers:
1. discovered -> applied succeeds
2. qualified -> applied succeeds
3. researched -> applied succeeds
4. contact_ready -> applied succeeds
5. contacted -> applied succeeds
6. application_date is automatically populated when missing
7. existing application_date is preserved
8. same Application ID remains unchanged
9. job_url/source/external_id remain unchanged
10. status_changed event is recorded
11. follow-up scheduling remains compatible
12. UI button calls the expected endpoint/action
13. no duplicate Application is created
"""
import asyncio
from datetime import date, timedelta
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, Settings
from app.models import (
    Application,
    ApplicationEvent,
    ApplicationStatus,
)
from app.services.lifecycle import schedule_follow_up, transition_status


def make_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def make_test_app(session, status: ApplicationStatus, application_date: date | None = None) -> Application:
    app = Application(
        company="ManualCorp",
        position="PFE Developer",
        source="manual",
        external_id="ext-12345",
        job_url="https://manualcorp.ma/jobs/pfe-1",
        application_status=status,
        application_date=application_date,
    )
    session.add(app)
    session.commit()
    return app


@pytest.fixture
def test_client():
    from fastapi.testclient import TestClient
    from app.main import app as fastapi_app
    from app.database import get_db

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(bind=engine)

    def override_get_db():
        try:
            db = TestingSessionLocal()
            yield db
        finally:
            db.close()

    fastapi_app.dependency_overrides[get_db] = override_get_db
    with TestClient(fastapi_app) as client:
        yield client
    fastapi_app.dependency_overrides.clear()


def test_transition_discovered_to_applied_succeeds():
    """Test 1: discovered -> applied succeeds."""
    session = make_session()
    app = make_test_app(session, ApplicationStatus.discovered)
    res = transition_status(session, app, ApplicationStatus.applied, notes="Manual application")
    assert res.application_status == ApplicationStatus.applied


def test_transition_qualified_to_applied_succeeds():
    """Test 2: qualified -> applied succeeds."""
    session = make_session()
    app = make_test_app(session, ApplicationStatus.qualified)
    res = transition_status(session, app, ApplicationStatus.applied)
    assert res.application_status == ApplicationStatus.applied


def test_transition_researched_to_applied_succeeds():
    """Test 3: researched -> applied succeeds."""
    session = make_session()
    app = make_test_app(session, ApplicationStatus.researched)
    res = transition_status(session, app, ApplicationStatus.applied)
    assert res.application_status == ApplicationStatus.applied


def test_transition_contact_ready_to_applied_succeeds():
    """Test 4: contact_ready -> applied succeeds."""
    session = make_session()
    app = make_test_app(session, ApplicationStatus.contact_ready)
    res = transition_status(session, app, ApplicationStatus.applied)
    assert res.application_status == ApplicationStatus.applied


def test_transition_contacted_to_applied_succeeds():
    """Test 5: contacted -> applied succeeds."""
    session = make_session()
    app = make_test_app(session, ApplicationStatus.contacted)
    res = transition_status(session, app, ApplicationStatus.applied)
    assert res.application_status == ApplicationStatus.applied


def test_application_date_automatically_populated_when_missing():
    """Test 6: application_date is automatically set to today when missing."""
    session = make_session()
    app = make_test_app(session, ApplicationStatus.discovered, application_date=None)
    assert app.application_date is None

    res = transition_status(session, app, ApplicationStatus.applied)
    assert res.application_date == date.today()


def test_existing_application_date_is_preserved():
    """Test 7: existing application_date is NOT overwritten."""
    session = make_session()
    past_date = date.today() - timedelta(days=5)
    app = make_test_app(session, ApplicationStatus.discovered, application_date=past_date)

    res = transition_status(session, app, ApplicationStatus.applied)
    assert res.application_date == past_date


def test_same_application_id_and_provenance_preserved():
    """Test 8 & 9: ID, job_url, source, external_id remain unchanged."""
    session = make_session()
    app = make_test_app(session, ApplicationStatus.discovered)
    original_id = app.id
    original_url = app.job_url
    original_source = app.source
    original_external_id = app.external_id

    res = transition_status(session, app, ApplicationStatus.applied)
    assert res.id == original_id
    assert res.job_url == original_url
    assert res.source == original_source
    assert res.external_id == original_external_id


def test_status_changed_event_is_recorded():
    """Test 10: status_changed timeline event is recorded."""
    session = make_session()
    app = make_test_app(session, ApplicationStatus.discovered)
    transition_status(session, app, ApplicationStatus.applied, notes="Manual apply via website")

    events = list(session.scalars(select(ApplicationEvent).where(ApplicationEvent.application_id == app.id)))
    assert any(e.event_type == "status_changed" and "discovered → applied" in (e.notes or "") for e in events)


def test_follow_up_scheduling_remains_compatible():
    """Test 11: applied application can be scheduled for follow-up."""
    session = make_session()
    app = make_test_app(session, ApplicationStatus.discovered)
    transition_status(session, app, ApplicationStatus.applied)

    settings = Settings(followup_delay_days_1=3)
    res = schedule_follow_up(session, app, settings)
    assert res.application_status == ApplicationStatus.waiting_response
    assert res.next_follow_up is not None


def test_ui_status_endpoint_updates_status_and_creates_no_duplicates(test_client):
    """Test 12 & 13: status endpoint succeeds for discovered app and creates 0 duplicates."""
    client = test_client
    ingest = client.post(
        "/opportunities/ingest",
        json={
            "source": "manual",
            "title": "PFE Developer",
            "company": "EndpointCorp",
            "url": "https://endpointcorp.ma/jobs/pfe",
        },
    )
    assert ingest.status_code == 200
    app_id = ingest.json()["id"]

    # Call status endpoint (simulating click on "J'ai envoyé / Mark as Applied")
    r = client.post(f"/applications/{app_id}/status", json={"status": "applied", "notes": "Manual application"})
    assert r.status_code == 200
    data = r.json()
    assert data["application_status"] == "applied"
    assert data["application_date"] == str(date.today())

    # Verify total application count is 1 (no duplicate created)
    apps = client.get("/applications").json()
    assert len(apps) == 1
    assert apps[0]["id"] == app_id
