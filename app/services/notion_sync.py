"""
Notion Sync Service — Phase 1: Notion -> SQLite import (read-only Notion side).

Design rules:
- dry_run=True: ZERO SQLite writes, ZERO Notion writes.
- dry_run=False: upsert opportunities + patch limited fields on matched records.
- Company name alone NEVER identifies an opportunity.
- Non-null existing SQLite values are NEVER overwritten by null/empty Notion values.
- notion_page_id is always stored/updated on matched records.
- SQLite -> Notion push is stubbed (Phase 2).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.integrations.notion_crm import NotionImportRecord, build_notion_provider
from app.models import Application, ApplicationEvent, ApplicationStatus
from app.scoring import score_opportunity

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result data classes
# ---------------------------------------------------------------------------

@dataclass
class NotionSyncResult:
    dry_run: bool
    pages_read: int = 0
    new_imported: int = 0
    matched_existing: int = 0
    skipped_empty: int = 0
    errors: list[str] = field(default_factory=list)
    preview: list[dict] = field(default_factory=list)  # populated in dry_run mode


# ---------------------------------------------------------------------------
# Identity matching helpers (mirrors upsert_opportunity logic)
# ---------------------------------------------------------------------------

def _find_existing(db: Session, record: NotionImportRecord) -> Application | None:
    """Find an existing Application matching this Notion record.

    Priority:
    1. notion_page_id exact match (fastest, most reliable after first import).
    2. canonical job URL.
    3. source + external_id (source="notion_crm", external_id=page_id).
    4. normalized company + title + location fingerprint.

    Company alone NEVER identifies an opportunity.
    """
    opp = record.opportunity
    page_data = record.page_data

    # 1. Previous import: notion_page_id stored on the SQLite record.
    existing = db.scalar(
        select(Application).where(Application.notion_page_id == page_data.notion_page_id)
    )
    if existing:
        return existing

    if opp is None:
        return None

    clauses = []

    # 2. Canonical job URL (only if it is a real job URL, not our synthetic notion.so fallback).
    url_str = str(opp.url)
    if "notion.so/" not in url_str:
        url_clean = url_str.rstrip("/")
        clauses.append(
            or_(
                Application.job_url == url_str,
                Application.job_url == url_clean,
                Application.job_url == url_clean + "/",
            )
        )

    # 3. source + external_id
    if opp.external_id:
        clauses.append(
            (Application.source == opp.source)
            & (Application.external_id == opp.external_id)
        )

    # 4. Normalized fingerprint: company + title + location
    norm_company = opp.company.strip().lower()
    norm_position = opp.title.strip().lower()
    norm_location = ""
    clauses.append(
        (func.lower(Application.company) == norm_company)
        & (func.lower(Application.position) == norm_position)
        & (func.lower(func.coalesce(Application.location, "")) == norm_location)
    )

    if not clauses:
        return None

    return db.scalar(select(Application).where(or_(*clauses)))


# ---------------------------------------------------------------------------
# Import core
# ---------------------------------------------------------------------------

def import_from_notion(
    db: Session,
    settings,
    dry_run: bool = True,
    preview_limit: int = 50,
) -> NotionSyncResult:
    """Import Notion CRM pages into SQLite.

    Args:
        db: SQLAlchemy session.
        settings: app Settings instance.
        dry_run: When True, performs zero writes. Returns preview of what would change.
        preview_limit: Max records included in the preview list.

    Returns:
        NotionSyncResult with counts and optional preview data.
    """
    result = NotionSyncResult(dry_run=dry_run)

    provider = build_notion_provider(settings)
    if provider is None:
        result.errors.append(
            "Notion not configured: NOTION_API_KEY and NOTION_DATABASE_ID must be set in .env"
        )
        return result

    # Fetch all pages from Notion (read-only).
    try:
        raw_pages = provider.fetch_all_pages()
    except RuntimeError as exc:
        result.errors.append(str(exc))
        return result

    result.pages_read = len(raw_pages)

    # Parse pages into import records.
    import_records = provider.parse_all_pages(raw_pages)

    for record in import_records:
        if record.skip_reason:
            result.skipped_empty += 1
            if len(result.preview) < preview_limit:
                result.preview.append({
                    "notion_page_id": record.page_data.notion_page_id,
                    "action": "SKIP",
                    "reason": record.skip_reason,
                    "title": record.page_data.position or record.page_data.notion_title,
                    "company": record.page_data.company,
                })
            continue

        opp = record.opportunity
        assert opp is not None  # guaranteed by skip_reason logic above

        # Determine action: NEW or MATCH.
        existing = _find_existing(db, record)

        if existing is not None:
            result.matched_existing += 1
            action = "MATCH"

            if not dry_run:
                # Update notion_page_id (always — this is the primary purpose).
                existing.notion_page_id = record.page_data.notion_page_id

                # Patch limited fields: only overwrite if Notion value is non-null.
                if record.mapped_status is not None and existing.application_status == ApplicationStatus.discovered:
                    # Only auto-update status if the record is still in the default state.
                    existing.application_status = record.mapped_status
                if record.mapped_contact_date is not None and existing.contact_date is None:
                    existing.contact_date = record.mapped_contact_date
                if record.mapped_last_contact is not None and existing.last_contact is None:
                    existing.last_contact = record.mapped_last_contact
                if record.mapped_next_follow_up is not None and existing.next_follow_up is None:
                    existing.next_follow_up = record.mapped_next_follow_up
                if record.mapped_follow_up_count is not None and existing.follow_up_count == 0:
                    existing.follow_up_count = record.mapped_follow_up_count
                # Append Notion notes without overwriting existing notes.
                if opp.notes:
                    current_notes = existing.notes or ""
                    if "source:notion_crm" not in current_notes:
                        existing.notes = (current_notes + " | " + opp.notes).strip(" |")

        else:
            result.new_imported += 1
            action = "NEW"

            if not dry_run:
                score_result = score_opportunity(opp)
                app_record = Application(
                    external_id=opp.external_id,
                    company=opp.company,
                    position=opp.title,
                    source=opp.source,
                    job_url=str(opp.url),
                    location=opp.location,
                    description=opp.description,
                    posted_at=opp.posted_at,
                    recruiter=opp.recruiter,
                    recruiter_linkedin_url=(
                        str(opp.recruiter_linkedin_url) if opp.recruiter_linkedin_url else None
                    ),
                    professional_email=opp.professional_email,
                    notes=opp.notes,
                    score=score_result.score,
                    score_reason=score_result.reason,
                    relevance_category=score_result.relevance_category,
                    notion_page_id=record.page_data.notion_page_id,
                    # Apply Notion-sourced dates if present.
                    contact_date=record.mapped_contact_date,
                    last_contact=record.mapped_last_contact,
                    next_follow_up=record.mapped_next_follow_up,
                    follow_up_count=record.mapped_follow_up_count or 0,
                )
                # Apply status from Notion if available.
                if record.mapped_status is not None:
                    app_record.application_status = record.mapped_status

                db.add(app_record)
                db.flush()
                db.add(
                    ApplicationEvent(
                        application_id=app_record.id,
                        event_type="imported_from_notion",
                        channel="notion_crm",
                        notes=f"Notion page {record.page_data.notion_page_id}",
                    )
                )

        if len(result.preview) < preview_limit:
            result.preview.append({
                "notion_page_id": record.page_data.notion_page_id,
                "action": action,
                "title": record.page_data.position,
                "company": record.page_data.company,
                "job_url": record.page_data.job_url,
                "notion_statut": record.page_data.notion_statut,
                "mapped_status": record.mapped_status.value if record.mapped_status else None,
                "tags": record.page_data.tags,
            })

    if not dry_run:
        try:
            db.commit()
            logger.info(
                "Notion import committed: new=%d, matched=%d, skipped=%d",
                result.new_imported, result.matched_existing, result.skipped_empty,
            )
        except Exception as exc:
            db.rollback()
            result.errors.append(f"DB commit error: {exc}")
            logger.error("Notion import DB commit failed: %s", exc)

    return result


# ---------------------------------------------------------------------------
# Phase 2 stub: SQLite -> Notion push
# ---------------------------------------------------------------------------

def push_to_notion(db: Session, settings, application_ids: list[int] | None = None) -> None:
    """SQLite -> Notion push synchronization.

    Phase 2 — NOT YET IMPLEMENTED.
    Raises NotImplementedError to prevent accidental invocation.
    """
    raise NotImplementedError(
        "Notion push (SQLite -> Notion) is Phase 2 and has not been implemented yet. "
        "Only Notion -> SQLite import is available."
    )
