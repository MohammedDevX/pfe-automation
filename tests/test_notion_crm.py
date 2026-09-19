import pytest
from unittest.mock import MagicMock, patch
from datetime import date

from fastapi.testclient import TestClient

from app.database import Base, engine, get_settings, SessionLocal
from app.main import app, ensure_additive_columns
from app.models import Application, ApplicationStatus
from app.schemas import OpportunityIn
from app.integrations.notion_crm import (
    NotionCRMProvider,
    build_notion_provider,
    map_notion_statut,
    map_notion_type,
)
from app.services.notion_sync import import_from_notion, push_to_notion


@pytest.fixture(autouse=True)
def setup_db():
    Base.metadata.create_all(bind=engine)
    ensure_additive_columns()
    db = SessionLocal()
    try:
        # Delete only mock test applications created during unit tests
        db.query(Application).filter(
            (Application.source == 'notion_crm') |
            (Application.notion_page_id.like('notion-%')) |
            (Application.company.like('%DryRun%')) |
            (Application.company.like('%TestCo%')) |
            (Application.company.like('%CloudCorp%')) |
            (Application.company.like('%PreserveCo%')) |
            (Application.company.like('%Acme%'))
        ).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()
    yield


# ---------------------------------------------------------------------------
# 1. Disabled when credentials absent
# ---------------------------------------------------------------------------
def test_notion_disabled_when_no_credentials():
    settings = get_settings()
    s_empty = settings.model_copy(update={'notion_api_key': None, 'notion_database_id': None})
    provider = build_notion_provider(s_empty)
    assert provider is None


# ---------------------------------------------------------------------------
# 2. Full mapping from Notion page properties
# ---------------------------------------------------------------------------
def test_page_to_opportunity_full_mapping():
    mock_page = {
        'id': 'page-uuid-1234',
        'properties': {
            'Contact': {'title': [{'plain_text': 'Stage PFE Full Stack'}]},
            'Entreprise': {'rich_text': [{'plain_text': 'Acme Corp'}]},
            'Offre': {'url': 'https://example.com/jobs/acme-pfe'},
            'LinkedIn': {'url': 'https://linkedin.com/in/recruiter-acme'},
            'Poste RH': {'rich_text': [{'plain_text': 'Talent Acquisition Specialist'}]},
            'Domaine / Stack': {'multi_select': [{'name': 'C#'}, {'name': 'Angular'}]},
            'Type': {'select': {'name': 'PFE'}},
            'Statut': {'select': {'name': 'Envoye'}},
            'Canal de candidature': {'select': {'name': 'LinkedIn'}},
            'Strategie': {'select': {'name': 'Direct'}},
            'CV utilise': {'select': {'name': 'CV_DotNet_Fr.pdf'}},
            'Date contact': {'date': {'start': '2026-03-01'}},
            'Date dernier message': {'date': {'start': '2026-03-05'}},
            'Prochaine relance': {'date': {'start': '2026-03-10'}},
            'Derniere action': {'rich_text': [{'plain_text': 'Email envoye'}]},
            'Reponse': {'rich_text': [{'plain_text': 'En attente de retour'}]},
            'Notes': {'rich_text': [{'plain_text': 'Candidature spontanee'}]},
            'Tentatives messages': {'number': 1},
        },
    }

    provider = NotionCRMProvider(api_key='secret_test', database_id='db_test_12345678')
    record = provider.parse_page(mock_page)

    assert record.skip_reason is None
    assert record.opportunity is not None
    assert record.opportunity.company == 'Acme Corp'
    assert record.opportunity.title == 'Acme Corp — Stage PFE Full Stack'
    assert str(record.opportunity.url).rstrip('/') == 'https://example.com/jobs/acme-pfe'
    assert record.opportunity.recruiter == 'Talent Acquisition Specialist'
    assert str(record.opportunity.recruiter_linkedin_url).rstrip('/') == 'https://linkedin.com/in/recruiter-acme'
    assert 'C#, Angular' in record.opportunity.notes
    assert record.mapped_status == ApplicationStatus.contacted
    assert record.mapped_contact_date == date(2026, 3, 1)
    assert record.mapped_last_contact == date(2026, 3, 5)
    assert record.mapped_next_follow_up == date(2026, 3, 10)
    assert record.mapped_follow_up_count == 1


# ---------------------------------------------------------------------------
# 3. Completely empty records skipped
# ---------------------------------------------------------------------------
def test_page_to_opportunity_missing_url_skipped():
    empty_page = {
        'id': 'page-empty',
        'properties': {
            'Contact': {'title': []},
            'Entreprise': {'rich_text': []},
            'Offre': {'url': None},
        },
    }
    provider = NotionCRMProvider(api_key='secret_test', database_id='db_test_12345678')
    record = provider.parse_page(empty_page)

    assert record.opportunity is None
    assert 'empty_record' in record.skip_reason


# ---------------------------------------------------------------------------
# 4. French status mapping to SQLite ApplicationStatus
# ---------------------------------------------------------------------------
def test_status_mapping_fr_to_sqlite():
    assert map_notion_statut('A envoyer') == ApplicationStatus.contact_ready
    assert map_notion_statut('Envoye') == ApplicationStatus.contacted
    assert map_notion_statut('En attente') == ApplicationStatus.contacted
    assert map_notion_statut('Candidature envoyee') == ApplicationStatus.applied
    assert map_notion_statut('Relance') == ApplicationStatus.follow_up_due
    assert map_notion_statut('Repondu') == ApplicationStatus.responded
    assert map_notion_statut('Entretien') == ApplicationStatus.interview
    assert map_notion_statut('Offre') == ApplicationStatus.offer
    assert map_notion_statut('Refuse') == ApplicationStatus.rejected
    assert map_notion_statut('Abandonne') == ApplicationStatus.withdrawn
    assert map_notion_statut('Ferme') == ApplicationStatus.closed
    assert map_notion_statut(None) is None


# ---------------------------------------------------------------------------
# 5. CRITICAL: Same company, different position = 2 SEPARATE records
# ---------------------------------------------------------------------------
def test_same_company_different_title_creates_two_records():
    db = SessionLocal()
    try:
        existing_app = Application(
            company='E-AMBITION',
            position='E-AMBITION — Stage Developpeur Angular',
            source='notion_crm',
            job_url='https://e-ambition.com/jobs/angular',
            application_status=ApplicationStatus.contacted,
        )
        db.add(existing_app)
        db.commit()

        mock_page_dotnet = {
            'id': 'notion-eambition-dotnet',
            'properties': {
                'Contact': {'title': [{'plain_text': 'Stage Developpeur .NET'}]},
                'Entreprise': {'rich_text': [{'plain_text': 'E-AMBITION'}]},
                'Offre': {'url': 'https://e-ambition.com/jobs/dotnet'},
                'Statut': {'select': {'name': 'En attente'}},
            },
        }

        settings = get_settings().model_copy(update={
            'notion_api_key': 'secret_test',
            'notion_database_id': 'db_test_12345678',
        })

        with patch('app.services.notion_sync.build_notion_provider') as mock_builder:
            mock_prov = MagicMock()
            mock_prov.fetch_all_pages.return_value = [mock_page_dotnet]
            p1 = NotionCRMProvider('secret_test', 'db_test_12345678').parse_page(mock_page_dotnet)
            mock_prov.parse_all_pages.return_value = [p1]
            mock_builder.return_value = mock_prov

            res = import_from_notion(db, settings, dry_run=False)

            assert res.new_imported == 1
            assert res.matched_existing == 0

            all_apps = db.query(Application).filter(Application.company == 'E-AMBITION').all()
            assert len(all_apps) == 2
            titles = {a.position for a in all_apps}
            assert 'E-AMBITION — Stage Developpeur Angular' in titles
            assert 'E-AMBITION — Stage Developpeur .NET' in titles
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 6. Same company + same job URL = deduplicates and matches
# ---------------------------------------------------------------------------
def test_same_company_same_url_deduplicates():
    db = SessionLocal()
    try:
        url = 'https://e-ambition.com/jobs/angular'
        existing_app = Application(
            company='E-AMBITION',
            position='E-AMBITION — Stage Developpeur Angular',
            source='stagiaires_ma',
            job_url=url,
            application_status=ApplicationStatus.discovered,
        )
        db.add(existing_app)
        db.commit()

        mock_page = {
            'id': 'notion-eambition-angular',
            'properties': {
                'Contact': {'title': [{'plain_text': 'Stage Developpeur Angular'}]},
                'Entreprise': {'rich_text': [{'plain_text': 'E-AMBITION'}]},
                'Offre': {'url': url},
                'Statut': {'select': {'name': 'Envoye'}},
            },
        }

        settings = get_settings().model_copy(update={
            'notion_api_key': 'secret_test',
            'notion_database_id': 'db_test_12345678',
        })

        with patch('app.services.notion_sync.build_notion_provider') as mock_builder:
            mock_prov = MagicMock()
            mock_prov.fetch_all_pages.return_value = [mock_page]
            p1 = NotionCRMProvider('secret_test', 'db_test_12345678').parse_page(mock_page)
            mock_prov.parse_all_pages.return_value = [p1]
            mock_builder.return_value = mock_prov

            res = import_from_notion(db, settings, dry_run=False)

            assert res.new_imported == 0
            assert res.matched_existing == 1

            db.refresh(existing_app)
            assert existing_app.notion_page_id == 'notion-eambition-angular'
            assert existing_app.application_status == ApplicationStatus.contacted
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 7. Dry run performs zero database writes
# ---------------------------------------------------------------------------
def test_import_dry_run_does_not_write():
    db = SessionLocal()
    try:
        count_before = db.query(Application).count()

        mock_page = {
            'id': 'notion-dryrun-1',
            'properties': {
                'Contact': {'title': [{'plain_text': 'Backend Dev'}]},
                'Entreprise': {'rich_text': [{'plain_text': 'DryRun Ltd'}]},
                'Offre': {'url': 'https://dryrun.example.com/job/1'},
            },
        }

        settings = get_settings().model_copy(update={
            'notion_api_key': 'secret_test',
            'notion_database_id': 'db_test_12345678',
        })

        with patch('app.services.notion_sync.build_notion_provider') as mock_builder:
            mock_prov = MagicMock()
            mock_prov.fetch_all_pages.return_value = [mock_page]
            p1 = NotionCRMProvider('secret_test', 'db_test_12345678').parse_page(mock_page)
            mock_prov.parse_all_pages.return_value = [p1]
            mock_builder.return_value = mock_prov

            res = import_from_notion(db, settings, dry_run=True)

            assert res.dry_run is True
            assert res.new_imported == 1

            count_after = db.query(Application).count()
            assert count_after == count_before
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 8. Live import persists new records
# ---------------------------------------------------------------------------
def test_import_creates_new_records():
    db = SessionLocal()
    try:
        mock_page = {
            'id': 'notion-new-1',
            'properties': {
                'Contact': {'title': [{'plain_text': 'DevOps Intern'}]},
                'Entreprise': {'rich_text': [{'plain_text': 'CloudCorp'}]},
                'Offre': {'url': 'https://cloudcorp.com/job/devops'},
            },
        }

        settings = get_settings().model_copy(update={
            'notion_api_key': 'secret_test',
            'notion_database_id': 'db_test_12345678',
        })

        with patch('app.services.notion_sync.build_notion_provider') as mock_builder:
            mock_prov = MagicMock()
            mock_prov.fetch_all_pages.return_value = [mock_page]
            p1 = NotionCRMProvider('secret_test', 'db_test_12345678').parse_page(mock_page)
            mock_prov.parse_all_pages.return_value = [p1]
            mock_builder.return_value = mock_prov

            res = import_from_notion(db, settings, dry_run=False)

            assert res.dry_run is False
            assert res.new_imported == 1

            app_rec = db.query(Application).filter(Application.notion_page_id == 'notion-new-1').first()
            assert app_rec is not None
            assert app_rec.company == 'CloudCorp'
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 9. Import is idempotent
# ---------------------------------------------------------------------------
def test_import_idempotent():
    db = SessionLocal()
    try:
        mock_page = {
            'id': 'notion-idem-1',
            'properties': {
                'Contact': {'title': [{'plain_text': 'QA Engineer'}]},
                'Entreprise': {'rich_text': [{'plain_text': 'TestCo'}]},
                'Offre': {'url': 'https://testco.com/job/qa'},
            },
        }

        settings = get_settings().model_copy(update={
            'notion_api_key': 'secret_test',
            'notion_database_id': 'db_test_12345678',
        })

        with patch('app.services.notion_sync.build_notion_provider') as mock_builder:
            mock_prov = MagicMock()
            mock_prov.fetch_all_pages.return_value = [mock_page]
            p1 = NotionCRMProvider('secret_test', 'db_test_12345678').parse_page(mock_page)
            mock_prov.parse_all_pages.return_value = [p1]
            mock_builder.return_value = mock_prov

            res1 = import_from_notion(db, settings, dry_run=False)
            assert res1.new_imported == 1

            res2 = import_from_notion(db, settings, dry_run=False)
            assert res2.new_imported == 0
            assert res2.matched_existing == 1
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 10. Null Notion fields do NOT overwrite existing SQLite values
# ---------------------------------------------------------------------------
def test_null_notion_fields_do_not_overwrite_sqlite():
    db = SessionLocal()
    try:
        url = 'https://example.com/jobs/preserve-test'
        existing_app = Application(
            company='PreserveCo',
            position='Developer',
            source='pfe_daba',
            job_url=url,
            contact_date=date(2026, 2, 1),
            application_status=ApplicationStatus.contacted,
        )
        db.add(existing_app)
        db.commit()

        mock_page = {
            'id': 'notion-preserve-1',
            'properties': {
                'Contact': {'title': [{'plain_text': 'Developer'}]},
                'Entreprise': {'rich_text': [{'plain_text': 'PreserveCo'}]},
                'Offre': {'url': url},
                'Date contact': {'date': None},
            },
        }

        settings = get_settings().model_copy(update={
            'notion_api_key': 'secret_test',
            'notion_database_id': 'db_test_12345678',
        })

        with patch('app.services.notion_sync.build_notion_provider') as mock_builder:
            mock_prov = MagicMock()
            mock_prov.fetch_all_pages.return_value = [mock_page]
            p1 = NotionCRMProvider('secret_test', 'db_test_12345678').parse_page(mock_page)
            mock_prov.parse_all_pages.return_value = [p1]
            mock_builder.return_value = mock_prov

            import_from_notion(db, settings, dry_run=False)

            db.refresh(existing_app)
            assert existing_app.contact_date == date(2026, 2, 1)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 11. Pagination handled
# ---------------------------------------------------------------------------
def test_notion_pagination_handled():
    with patch('notion_client.Client') as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        mock_client.databases.query.side_effect = [
            {
                'results': [{'id': 'p1', 'properties': {}}],
                'has_more': True,
                'next_cursor': 'cursor_2',
            },
            {
                'results': [{'id': 'p2', 'properties': {}}],
                'has_more': False,
                'next_cursor': None,
            },
        ]

        provider = NotionCRMProvider('secret_test', 'db_test_12345678')
        pages = provider.fetch_all_pages()

        assert len(pages) == 2
        assert pages[0]['id'] == 'p1'
        assert pages[1]['id'] == 'p2'


# ---------------------------------------------------------------------------
# 12. REST API /notion/status
# ---------------------------------------------------------------------------
def test_api_notion_status_endpoint():
    client = TestClient(app)
    res = client.get('/notion/status')
    assert res.status_code == 200
    data = res.json()
    assert 'configured' in data


# ---------------------------------------------------------------------------
# 13. REST API /notion/import (dry_run=true)
# ---------------------------------------------------------------------------
def test_api_notion_import_dry_run():
    client = TestClient(app)
    with patch('app.main.import_from_notion') as mock_sync:
        mock_res = MagicMock()
        mock_res.dry_run = True
        mock_res.pages_read = 10
        mock_res.new_imported = 4
        mock_res.matched_existing = 6
        mock_res.skipped_empty = 0
        mock_res.errors = []
        mock_res.preview = []
        mock_sync.return_value = mock_res

        res = client.post('/notion/import?dry_run=true')
        assert res.status_code == 200
        data = res.json()
        assert data['dry_run'] is True
        assert data['pages_read'] == 10


# ---------------------------------------------------------------------------
# Phase 2 stub verification: push_to_notion raises NotImplementedError
# ---------------------------------------------------------------------------
def test_push_to_notion_raises_not_implemented():
    db = SessionLocal()
    try:
        with pytest.raises(NotImplementedError):
            push_to_notion(db, get_settings())
    finally:
        db.close()
