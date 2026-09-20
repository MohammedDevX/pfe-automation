import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Application
from app.schemas import OpportunityIn
from app.services.opportunities import upsert_opportunity
from app.utils import canonicalize_url


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def test_canonicalize_url_trailing_slash():
    """A. Trailing slash: stored URL without slash vs incoming with slash -> same canonical URL."""
    url1 = "https://example.com/jobs/123"
    url2 = "https://example.com/jobs/123/"
    assert canonicalize_url(url1) == canonicalize_url(url2)
    assert canonicalize_url(url2) == "https://example.com/jobs/123"


def test_canonicalize_url_tracking_query_param():
    """B. Tracking query parameter: utm_source, refId, lever-origin removed."""
    url1 = "https://example.com/jobs/123"
    url2 = "https://example.com/jobs/123?utm_source=test&ref=xyz&lever-origin=applied"
    assert canonicalize_url(url1) == canonicalize_url(url2)


def test_canonicalize_url_provider_identity_param_preserved():
    """C. Provider identity parameter: gh_jid must NOT be stripped."""
    url = "https://careers.datadoghq.com/detail/8181440/?gh_jid=8181440"
    canon = canonicalize_url(url)
    assert "gh_jid=8181440" in canon
    # Verify that different gh_jid results in different canonical URLs
    url_other = "https://careers.datadoghq.com/detail/8181440/?gh_jid=9999999"
    assert canonicalize_url(url) != canonicalize_url(url_other)


def test_upsert_opportunity_canonical_url_variation(db_session):
    """E. Existing DB record stored with trailing slash / query param matches incoming variation."""
    # Stored record with trailing slash and tracking param
    app_stored = Application(
        company="Datadog",
        position="Backend Intern",
        source="greenhouse:datadog",
        job_url="https://careers.datadoghq.com/detail/8181440/?gh_jid=8181440&utm_source=linkedin",
        location="Paris",
        score=80,
    )
    db_session.add(app_stored)
    db_session.commit()

    # Incoming opportunity without tracking param and without trailing slash
    incoming = OpportunityIn(
        source="greenhouse:datadog",
        title="Backend Intern",
        company="Datadog",
        url="https://careers.datadoghq.com/detail/8181440?gh_jid=8181440",
        location="Paris",
    )

    app_upserted, was_created = upsert_opportunity(db_session, incoming)
    assert was_created is False
    assert app_upserted.id == app_stored.id


def test_same_canonical_url_in_one_batch_with_different_titles(db_session):
    """D. Same canonical URL in batch with different titles handled by seen_urls and upsert."""
    first = OpportunityIn(
        source="greenhouse:test",
        title="Software Engineer Intern",
        company="Company A",
        url="https://job-boards.greenhouse.io/comp/jobs/100",
        location="Paris",
    )
    second = OpportunityIn(
        source="greenhouse:test",
        title="Software Engineering Intern (PFE)",
        company="Company A Corp",
        url="https://job-boards.greenhouse.io/comp/jobs/100/",
        location="Paris, France",
    )

    app1, created1 = upsert_opportunity(db_session, first)
    db_session.flush()
    app2, created2 = upsert_opportunity(db_session, second)
    db_session.flush()

    assert created1 is True
    assert created2 is False
    assert app1.id == app2.id


def test_duplicate_integrity_error_handled_by_savepoint(db_session):
    """F. Simulated IntegrityError on db.flush() is caught by savepoint and handled without crashing."""
    # Add initial app
    app1 = Application(
        company="Acme",
        position="Dev",
        source="lever:acme",
        external_id="ext-123",
        job_url="https://jobs.lever.co/acme/123",
        score=75,
    )
    db_session.add(app1)
    db_session.commit()

    # Incoming opportunity that would collide on (source, external_id) constraint
    incoming = OpportunityIn(
        source="lever:acme",
        external_id="ext-123",
        title="Dev PFE",
        company="Acme Corp",
        url="https://jobs.lever.co/acme/123-alt",
        location="Remote",
    )

    app_res, was_created = upsert_opportunity(db_session, incoming)
    assert was_created is False
    assert app_res.id == app1.id


def test_unrelated_integrity_error_propagates(db_session):
    """G. Unrelated IntegrityError (e.g. invalid foreign key or missing field) still propagates."""
    # Create opportunity that will trigger an actual unresolvable IntegrityError if flushed with null required field
    # We test this by patching _find_existing_application to return None even after error
    from unittest.mock import patch
    incoming = OpportunityIn(
        source="test",
        title="Title",
        company="Company",
        url="https://example.com/unique-url-err",
    )
    
    # Force a failure during flush where no existing application exists
    with patch("app.services.opportunities._find_existing_application", side_effect=[None, None]):
        with patch.object(db_session, "begin_nested") as mock_nested:
            mock_nested.side_effect = IntegrityError("NOT NULL constraint failed", params=None, orig=Exception("NOT NULL"))
            with pytest.raises(IntegrityError):
                upsert_opportunity(db_session, incoming)


def test_provider_external_id_duplicate_detected(db_session):
    """H. Provider / external_id duplicate remains correctly detected."""
    app_stored = Application(
        company="Beta",
        position="Fullstack Intern",
        source="lever:beta",
        external_id="beta-999",
        job_url="https://jobs.lever.co/beta/original-url",
        score=70,
    )
    db_session.add(app_stored)
    db_session.commit()

    incoming = OpportunityIn(
        source="lever:beta",
        external_id="beta-999",
        title="Fullstack Intern PFE",
        company="Beta Inc",
        url="https://jobs.lever.co/beta/new-url",
        location="Casablanca",
    )

    app_res, was_created = upsert_opportunity(db_session, incoming)
    assert was_created is False
    assert app_res.id == app_stored.id


def test_migrated_86_applications_compatibility(db_session):
    """I. Existing migrated applications remain compatible with canonical matching."""
    sample_migrated_urls = [
        "https://job-boards.greenhouse.io/doctolib/jobs/7997192003",
        "https://careers.datadoghq.com/detail/8181440/?gh_jid=8181440",
        "https://jobs.lever.co/blablacar/517fe4e3-2611-4fed-933c-ef0455b46aad",
        "https://stagiaires.ma/stage-emploi-maroc/6532",
    ]
    for i, url in enumerate(sample_migrated_urls, 1):
        db_session.add(
            Application(
                company=f"Company {i}",
                position=f"Position {i}",
                source="test",
                job_url=url,
                score=60,
            )
        )
    db_session.commit()

    # Test querying with variation of each URL
    variations = [
        "https://job-boards.greenhouse.io/doctolib/jobs/7997192003/",
        "https://careers.datadoghq.com/detail/8181440?gh_jid=8181440",
        "https://jobs.lever.co/blablacar/517fe4e3-2611-4fed-933c-ef0455b46aad?utm_source=test",
        "https://stagiaires.ma/stage-emploi-maroc/6532/",
    ]

    for i, var_url in enumerate(variations, 1):
        opp = OpportunityIn(
            source="test",
            title=f"Position {i}",
            company=f"Company {i}",
            url=var_url,
        )
        app_found, created = upsert_opportunity(db_session, opp)
        assert created is False
        assert app_found.company == f"Company {i}"
