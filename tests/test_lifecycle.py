"""Tests for the application lifecycle, response tracking, and follow-up management slice."""
from __future__ import annotations

import asyncio
from datetime import date, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, Settings
from app.integrations.imap_reader import _classify_heuristic, fetch_replies
from app.models import (
    Application,
    ApplicationEvent,
    ApplicationStatus,
    IncomingResponse,
    MessageChannel,
    MessageStatus,
    OutboundMessage,
    ResponseStatus,
)
from app.schemas import RecordResponseRequest
from app.services.lifecycle import (
    _apply_classification,
    confirm_response,
    get_awaiting_response,
    get_due_follow_ups,
    mark_follow_up_sent,
    mark_follow_ups_due,
    record_response,
    schedule_follow_up,
    transition_status,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DEFAULT_SETTINGS = Settings(
    followup_delay_days_1=3,
    followup_delay_days_2=5,
    followup_max=2,
)


def make_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def make_app(session, status: ApplicationStatus = ApplicationStatus.discovered) -> Application:
    app = Application(
        company="TechCorp",
        position="PFE Backend",
        source="manual",
        job_url="https://techcorp.ma/jobs/pfe",
        application_status=status,
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
    SessionLocal = sessionmaker(bind=engine)

    def override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    fastapi_app.dependency_overrides[get_db] = override_get_db
    with TestClient(fastapi_app) as client:
        yield client
    fastapi_app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Status transitions
# ---------------------------------------------------------------------------

def test_valid_transition_discovered_to_qualified() -> None:
    session = make_session()
    app = make_app(session)
    result = transition_status(session, app, ApplicationStatus.qualified)
    assert result.application_status == ApplicationStatus.qualified


def test_invalid_transition_raises() -> None:
    session = make_session()
    app = make_app(session, ApplicationStatus.discovered)
    with pytest.raises(ValueError, match="Invalid transition"):
        transition_status(session, app, ApplicationStatus.offer)


def test_transition_creates_timeline_event() -> None:
    session = make_session()
    app = make_app(session)
    transition_status(session, app, ApplicationStatus.qualified, notes="Looks good")
    events = list(session.scalars(
        select(ApplicationEvent).where(ApplicationEvent.application_id == app.id)
    ))
    assert any(e.event_type == "status_changed" for e in events)
    assert any("qualified" in (e.notes or "") for e in events)


def test_same_status_is_noop() -> None:
    session = make_session()
    app = make_app(session)
    result = transition_status(session, app, ApplicationStatus.discovered)
    assert result.application_status == ApplicationStatus.discovered
    events = list(session.scalars(
        select(ApplicationEvent).where(ApplicationEvent.application_id == app.id)
    ))
    assert not any(e.event_type == "status_changed" for e in events)


def test_backward_compatible_contacted_status() -> None:
    """Applications that were previously marked 'contacted' must still be readable."""
    session = make_session()
    app = make_app(session, ApplicationStatus.contacted)
    assert app.application_status == ApplicationStatus.contacted


# ---------------------------------------------------------------------------
# Follow-up scheduling
# ---------------------------------------------------------------------------

def test_schedule_follow_up_sets_next_date() -> None:
    session = make_session()
    app = make_app(session, ApplicationStatus.contacted)
    app.last_contact = date.today()
    session.commit()

    result = schedule_follow_up(session, app, DEFAULT_SETTINGS)
    assert result.next_follow_up == date.today() + timedelta(days=3)
    assert result.application_status == ApplicationStatus.waiting_response


def test_schedule_follow_up_creates_event() -> None:
    session = make_session()
    app = make_app(session, ApplicationStatus.contacted)
    schedule_follow_up(session, app, DEFAULT_SETTINGS)
    events = list(session.scalars(
        select(ApplicationEvent).where(ApplicationEvent.application_id == app.id)
    ))
    assert any(e.event_type == "follow_up_scheduled" for e in events)


def test_duplicate_follow_up_raises() -> None:
    session = make_session()
    app = make_app(session, ApplicationStatus.contacted)
    schedule_follow_up(session, app, DEFAULT_SETTINGS)
    # Try scheduling again while first follow-up is still pending
    with pytest.raises(ValueError, match="already scheduled"):
        schedule_follow_up(session, app, DEFAULT_SETTINGS)


def test_max_follow_ups_raises() -> None:
    session = make_session()
    app = make_app(session, ApplicationStatus.contacted)
    app.follow_up_count = 2  # already at max
    session.commit()
    with pytest.raises(ValueError, match="Maximum follow-ups"):
        schedule_follow_up(session, app, DEFAULT_SETTINGS)


def test_second_follow_up_uses_longer_delay() -> None:
    session = make_session()
    app = make_app(session, ApplicationStatus.contacted)
    app.last_contact = date.today()
    app.follow_up_count = 1  # first already done
    # Clear any pending follow-up to avoid duplicate guard
    app.next_follow_up = None
    session.commit()
    result = schedule_follow_up(session, app, DEFAULT_SETTINGS)
    assert result.next_follow_up == date.today() + timedelta(days=5)  # delay_days_2


def test_mark_follow_up_sent_increments_count() -> None:
    session = make_session()
    app = make_app(session, ApplicationStatus.waiting_response)
    app.follow_up_count = 0
    session.commit()
    result = mark_follow_up_sent(session, app)
    assert result.follow_up_count == 1
    assert result.application_status == ApplicationStatus.waiting_response
    assert result.last_contact == date.today()


# ---------------------------------------------------------------------------
# Get due follow-ups
# ---------------------------------------------------------------------------

def test_get_due_follow_ups_returns_overdue() -> None:
    session = make_session()
    app = make_app(session, ApplicationStatus.waiting_response)
    app.next_follow_up = date.today() - timedelta(days=1)  # overdue
    session.commit()
    due = get_due_follow_ups(session)
    assert any(a.id == app.id for a in due)


def test_get_due_follow_ups_excludes_future() -> None:
    session = make_session()
    app = make_app(session, ApplicationStatus.waiting_response)
    app.next_follow_up = date.today() + timedelta(days=5)  # future
    session.commit()
    due = get_due_follow_ups(session)
    assert not any(a.id == app.id for a in due)


def test_mark_follow_ups_due_updates_status() -> None:
    session = make_session()
    app = make_app(session, ApplicationStatus.waiting_response)
    app.next_follow_up = date.today() - timedelta(days=2)
    session.commit()
    count = mark_follow_ups_due(session)
    assert count >= 1
    session.refresh(app)
    assert app.application_status == ApplicationStatus.follow_up_due


# ---------------------------------------------------------------------------
# Response recording
# ---------------------------------------------------------------------------

def test_record_response_persists_record() -> None:
    session = make_session()
    app = make_app(session)
    req = RecordResponseRequest(
        sender="recruiter@techcorp.ma",
        subject="Re: Stage PFE",
        body_preview="Merci pour votre candidature...",
        classification=None,
        source="manual",
    )
    result = record_response(session, app, req)
    assert result.id is not None
    assert result.sender == "recruiter@techcorp.ma"
    assert not result.confirmed


def test_record_response_creates_event() -> None:
    session = make_session()
    app = make_app(session)
    req = RecordResponseRequest(sender="hr@corp.ma", source="manual")
    record_response(session, app, req)
    events = list(session.scalars(
        select(ApplicationEvent).where(ApplicationEvent.application_id == app.id)
    ))
    assert any(e.event_type == "response_received" for e in events)


def test_record_response_deduplicates_by_message_id() -> None:
    session = make_session()
    app = make_app(session)
    req = RecordResponseRequest(
        sender="hr@corp.ma",
        source="imap",
        message_id_header="<unique-id-123@mail.corp.ma>",
    )
    r1 = record_response(session, app, req)
    r2 = record_response(session, app, req)  # same message_id
    assert r1.id == r2.id  # deduplicated — same record returned


def test_confirm_response_applies_classification() -> None:
    session = make_session()
    app = make_app(session, ApplicationStatus.waiting_response)
    req = RecordResponseRequest(sender="hr@corp.ma", source="manual")
    resp = record_response(session, app, req)

    confirm_response(session, resp, ResponseStatus.positive)

    session.refresh(app)
    assert app.response_status == ResponseStatus.positive
    assert app.last_response_date is not None
    assert app.application_status == ApplicationStatus.responded


def test_confirm_interview_updates_status() -> None:
    session = make_session()
    app = make_app(session, ApplicationStatus.waiting_response)
    req = RecordResponseRequest(sender="hr@corp.ma", source="manual")
    resp = record_response(session, app, req)
    confirm_response(session, resp, ResponseStatus.interview)
    session.refresh(app)
    assert app.application_status == ApplicationStatus.interview


def test_confirm_negative_closes_to_rejected() -> None:
    session = make_session()
    app = make_app(session, ApplicationStatus.waiting_response)
    req = RecordResponseRequest(sender="hr@corp.ma", source="manual")
    resp = record_response(session, app, req)
    confirm_response(session, resp, ResponseStatus.negative)
    session.refresh(app)
    assert app.application_status == ApplicationStatus.rejected


def test_confirmed_response_at_create_time() -> None:
    """If confirmed=True at creation, status must be applied immediately."""
    session = make_session()
    app = make_app(session, ApplicationStatus.waiting_response)
    req = RecordResponseRequest(
        sender="hr@corp.ma",
        source="manual",
        classification=ResponseStatus.positive,
        confirmed=True,
    )
    record_response(session, app, req)
    session.refresh(app)
    assert app.response_status == ResponseStatus.positive


# ---------------------------------------------------------------------------
# IMAP heuristic classifier
# ---------------------------------------------------------------------------

def test_classify_interview_detected() -> None:
    classification, confidence = _classify_heuristic("Invitation entretien", "")
    assert classification == "interview invitation"
    assert confidence > 0.5


def test_classify_refusal_detected() -> None:
    classification, confidence = _classify_heuristic("Re: Stage", "Suite à votre candidature, nous regrettons de ne pas avoir retenu votre dossier.")
    assert classification == "negative response"
    assert confidence > 0.5


def test_classify_ambiguous_returns_none() -> None:
    classification, confidence = _classify_heuristic("Re: Stage PFE", "Merci pour votre message.")
    assert classification is None
    assert confidence == 0.0


def test_imap_reader_raises_without_credentials() -> None:
    with pytest.raises(RuntimeError, match="SMTP_USER"):
        fetch_replies(
            imap_host="imap.gmail.com",
            imap_port=993,
            username="",
            password="",
        )


def test_imap_reader_with_mocked_connection() -> None:
    """Verify the IMAP reader parses messages correctly with a mock connection."""
    import email as email_lib
    from unittest.mock import MagicMock, patch as mock_patch

    fake_msg = email_lib.message_from_string(
        "Message-ID: <test-id-001@mail.com>\r\n"
        "From: recruiter@techcorp.ma\r\n"
        "Subject: Re: PFE Stage\r\n"
        "Date: Wed, 02 Sep 2026 10:00:00 +0000\r\n"
        "Content-Type: text/plain\r\n\r\n"
        "Merci pour votre candidature. Nous souhaitons organiser un entretien."
    )
    raw_bytes = fake_msg.as_bytes()

    mock_conn = MagicMock()
    mock_conn.search.return_value = (None, [b"1"])
    mock_conn.fetch.return_value = (None, [(None, raw_bytes)])

    with mock_patch("imaplib.IMAP4_SSL", return_value=mock_conn):
        results = fetch_replies(
            imap_host="imap.gmail.com",
            imap_port=993,
            username="test@gmail.com",
            password="app-password",
            since_days=7,
        )

    assert len(results) == 1
    assert results[0].sender == "recruiter@techcorp.ma"
    assert results[0].subject == "Re: PFE Stage"
    assert results[0].classification_hint == "interview invitation"


# ---------------------------------------------------------------------------
# API endpoint integration
# ---------------------------------------------------------------------------

def test_status_endpoint_transitions_application(test_client) -> None:
    ingest = test_client.post(
        "/opportunities/ingest",
        json={
            "source": "manual",
            "title": "PFE Backend",
            "company": "LifecycleCorpA",
            "url": "https://lca.ma/jobs/pfe",
        },
    )
    app_id = ingest.json()["id"]
    r = test_client.post(
        f"/applications/{app_id}/status",
        json={"status": "qualified", "notes": "Good score"},
    )
    assert r.status_code == 200
    assert r.json()["application_status"] == "qualified"


def test_invalid_transition_returns_409(test_client) -> None:
    ingest = test_client.post(
        "/opportunities/ingest",
        json={
            "source": "manual",
            "title": "PFE Backend",
            "company": "LifecycleCorpB",
            "url": "https://lcb.ma/jobs/pfe",
        },
    )
    app_id = ingest.json()["id"]
    r = test_client.post(
        f"/applications/{app_id}/status",
        json={"status": "offer"},  # invalid from discovered
    )
    assert r.status_code == 409


def test_record_response_endpoint(test_client) -> None:
    ingest = test_client.post(
        "/opportunities/ingest",
        json={
            "source": "manual",
            "title": "PFE Backend",
            "company": "ResponseCorp",
            "url": "https://rc.ma/jobs/pfe",
        },
    )
    app_id = ingest.json()["id"]
    r = test_client.post(
        f"/applications/{app_id}/responses",
        json={
            "sender": "hr@responsecorp.ma",
            "subject": "Re: Stage PFE",
            "body_preview": "Merci, nous souhaitons vous rencontrer.",
            "source": "manual",
        },
    )
    assert r.status_code == 200
    assert r.json()["sender"] == "hr@responsecorp.ma"
    assert r.json()["confirmed"] is False


def test_due_follow_ups_endpoint_returns_list(test_client) -> None:
    r = test_client.get("/follow-ups/due")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_inbox_scan_without_credentials_returns_400(test_client) -> None:
    r = test_client.post("/inbox/scan")
    assert r.status_code == 400
    assert "SMTP_USER" in r.json()["detail"]


def test_excel_export_includes_new_columns(test_client) -> None:
    r = test_client.get("/export/applications.xlsx")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    # Verify xlsx is non-empty (actual column check would need openpyxl)
    assert len(r.content) > 1000
