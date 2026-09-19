"""
Notion CRM Provider — Read-only Phase 1 integration.

Reads pages from the Notion database "PFE — Job Search CRM" and maps each
page's properties to an OpportunityIn + metadata dict.

Design rules:
- NEVER writes to Notion.
- NEVER logs or exposes the API key.
- Gracefully disabled when NOTION_API_KEY or NOTION_DATABASE_ID are absent.
- Company name alone NEVER identifies an opportunity.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from app.models import ApplicationStatus
from app.schemas import OpportunityIn

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes for raw Notion -> mapped result
# ---------------------------------------------------------------------------

@dataclass
class NotionPageData:
    """Parsed data extracted from one Notion page."""
    notion_page_id: str
    notion_title: str           # raw "Contact" property value (display label)
    company: str                # Notion "Entreprise"
    position: str               # derived display title
    job_url: str | None         # Notion "Offre"
    recruiter_linkedin_url: str | None
    recruiter_role: str | None
    tags: list[str]             # Notion "Domaine / Stack"
    notion_type: str | None
    notion_statut: str | None
    canal: str | None
    strategie: str | None
    cv_utilise: str | None
    date_contact: date | None
    date_dernier_message: date | None
    prochaine_relance: date | None
    derniere_action: str | None
    reponse: str | None
    notes: str | None
    tentatives_messages: int | None


@dataclass
class NotionImportRecord:
    """Result of mapping one Notion page into the import pipeline."""
    page_data: NotionPageData
    opportunity: OpportunityIn | None   # None -> skipped
    skip_reason: str | None = None
    mapped_status: ApplicationStatus | None = None
    mapped_contact_date: date | None = None
    mapped_last_contact: date | None = None
    mapped_next_follow_up: date | None = None
    mapped_follow_up_count: int | None = None


# ---------------------------------------------------------------------------
# Status mapping: Notion "Statut" (French) -> ApplicationStatus
# ---------------------------------------------------------------------------

_STATUT_MAP: dict[str, ApplicationStatus] = {
    "a envoyer": ApplicationStatus.contact_ready,
    "a contacter": ApplicationStatus.contact_ready,
    "pret a envoyer": ApplicationStatus.contact_ready,
    "envoye": ApplicationStatus.contacted,
    "en attente": ApplicationStatus.contacted,
    "candidature envoyee": ApplicationStatus.applied,
    "postule": ApplicationStatus.applied,
    "relance": ApplicationStatus.follow_up_due,
    "relance 1": ApplicationStatus.follow_up_due,
    "relance 2": ApplicationStatus.follow_up_due,
    "repondu": ApplicationStatus.responded,
    "reponse recue": ApplicationStatus.responded,
    "entretien": ApplicationStatus.interview,
    "entretien planifie": ApplicationStatus.interview,
    "offre": ApplicationStatus.offer,
    "offre recue": ApplicationStatus.offer,
    "refuse": ApplicationStatus.rejected,
    "rejete": ApplicationStatus.rejected,
    "pas de suite": ApplicationStatus.rejected,
    "abandonne": ApplicationStatus.withdrawn,
    "retire": ApplicationStatus.withdrawn,
    "ferme": ApplicationStatus.closed,
    "cloture": ApplicationStatus.closed,
}


def _normalise_fr(text: str) -> str:
    """Lowercase + strip accents for robust French status matching."""
    import unicodedata
    nfkd = unicodedata.normalize("NFKD", text.lower().strip())
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def map_notion_statut(statut: str | None) -> ApplicationStatus | None:
    """Map a Notion French Statut value to SQLite ApplicationStatus.
    Returns None when statut is absent so existing SQLite value is preserved.
    """
    if not statut:
        return None
    key = _normalise_fr(statut)
    return _STATUT_MAP.get(key)


def map_notion_type(type_val: str | None) -> str | None:
    """Map Notion 'Type' select to relevance_category string."""
    if not type_val:
        return None
    t = type_val.strip().lower()
    if "pfe" in t or "fin d" in t:
        return "EXPLICIT_PFE"
    if "stage" in t or "intern" in t or "alternance" in t:
        return "EXPLICIT_INTERNSHIP"
    if "junior" in t:
        return "JUNIOR"
    return None


# ---------------------------------------------------------------------------
# Property extraction helpers
# ---------------------------------------------------------------------------

def _extract_title(prop: dict | None) -> str:
    if not prop:
        return ""
    parts = prop.get("title", []) or []
    return "".join(p.get("plain_text", "") for p in parts).strip()


def _extract_rich_text(prop: dict | None) -> str | None:
    if not prop:
        return None
    parts = prop.get("rich_text", []) or []
    text = "".join(p.get("plain_text", "") for p in parts).strip()
    return text or None


def _extract_url(prop: dict | None) -> str | None:
    if not prop:
        return None
    val = prop.get("url")
    return val.strip() if val else None


def _extract_select(prop: dict | None) -> str | None:
    if not prop:
        return None
    sel = prop.get("select")
    if not sel:
        return None
    return sel.get("name", "").strip() or None


def _extract_multi_select(prop: dict | None) -> list[str]:
    if not prop:
        return []
    options = prop.get("multi_select", []) or []
    return [o.get("name", "").strip() for o in options if o.get("name")]


def _extract_date(prop: dict | None) -> date | None:
    if not prop:
        return None
    date_obj = prop.get("date")
    if not date_obj:
        return None
    start = date_obj.get("start")
    if not start:
        return None
    try:
        return datetime.fromisoformat(start.split("T")[0]).date()
    except (ValueError, TypeError):
        return None


def _extract_number(prop: dict | None) -> int | None:
    if not prop:
        return None
    val = prop.get("number")
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Main provider class
# ---------------------------------------------------------------------------

class NotionCRMProvider:
    """Read-only provider that fetches and maps Notion CRM pages.

    Phase 1: Notion -> SQLite import only. Never modifies Notion.
    The API key is consumed once during construction and never logged.
    """

    def __init__(self, api_key: str, database_id: str, batch_size: int = 100):
        self._database_id = database_id
        self._batch_size = min(batch_size, 100)
        self._client = self._build_client(api_key)

    @staticmethod
    def _build_client(api_key: str):
        from notion_client import Client
        return Client(auth=api_key)

    def __repr__(self) -> str:
        return "NotionCRMProvider(database_id=***masked***)"

    def fetch_all_pages(self) -> list[dict]:
        """Fetch all pages from the Notion database using pagination."""
        pages: list[dict] = []
        cursor: str | None = None

        while True:
            kwargs: dict[str, Any] = {
                "database_id": self._database_id,
                "page_size": self._batch_size,
            }
            if cursor:
                kwargs["start_cursor"] = cursor
            try:
                response = self._client.databases.query(**kwargs)
            except Exception as exc:
                safe_msg = str(exc).replace(self._database_id, "***db_id***")
                logger.error("Notion API error: %s", safe_msg)
                raise RuntimeError(f"Notion API fetch failed: {safe_msg}") from exc

            batch: list[dict] = response.get("results", [])
            pages.extend(batch)
            has_more = response.get("has_more", False)
            cursor = response.get("next_cursor")
            if not has_more or not cursor:
                break

        logger.info("Notion: fetched %d pages total", len(pages))
        return pages

    def parse_page(self, page: dict) -> NotionImportRecord:
        """Parse one raw Notion page into a NotionImportRecord."""
        page_id: str = page.get("id", "")
        props: dict = page.get("properties", {})

        notion_title = _extract_title(props.get("Contact"))
        company_raw = _extract_rich_text(props.get("Entreprise")) or ""
        job_url = _extract_url(props.get("Offre"))
        recruiter_linkedin = _extract_url(props.get("LinkedIn"))
        recruiter_role = _extract_rich_text(props.get("Poste RH"))
        tags = _extract_multi_select(props.get("Domaine / Stack"))
        notion_type = _extract_select(props.get("Type"))
        notion_statut = _extract_select(props.get("Statut"))
        canal = _extract_select(props.get("Canal de candidature"))
        strategie = _extract_select(props.get("Strategie"))
        cv_utilise = _extract_select(props.get("CV utilise"))
        date_contact = _extract_date(props.get("Date contact"))
        date_dernier_message = _extract_date(props.get("Date dernier message"))
        prochaine_relance = _extract_date(props.get("Prochaine relance"))
        derniere_action = _extract_rich_text(props.get("Derniere action"))
        reponse = _extract_rich_text(props.get("Reponse"))
        notes_raw = _extract_rich_text(props.get("Notes"))
        tentatives = _extract_number(props.get("Tentatives messages"))

        company = company_raw.strip()
        if company and notion_title:
            if notion_title.lower().startswith(company.lower()):
                position = notion_title
            else:
                position = f"{company} — {notion_title}"
        elif notion_title:
            position = notion_title
        else:
            position = company or "(sans titre)"

        page_data = NotionPageData(
            notion_page_id=page_id,
            notion_title=notion_title,
            company=company,
            position=position,
            job_url=job_url,
            recruiter_linkedin_url=recruiter_linkedin,
            recruiter_role=recruiter_role,
            tags=tags,
            notion_type=notion_type,
            notion_statut=notion_statut,
            canal=canal,
            strategie=strategie,
            cv_utilise=cv_utilise,
            date_contact=date_contact,
            date_dernier_message=date_dernier_message,
            prochaine_relance=prochaine_relance,
            derniere_action=derniere_action,
            reponse=reponse,
            notes=notes_raw,
            tentatives_messages=tentatives,
        )

        if not job_url and not company and not notion_title:
            return NotionImportRecord(
                page_data=page_data,
                opportunity=None,
                skip_reason="empty_record: no URL, company, or title",
            )

        notes_parts: list[str] = []
        if tags:
            notes_parts.append(f"tags: {', '.join(tags)}")
        if cv_utilise:
            notes_parts.append(f"cv_used: {cv_utilise}")
        if canal:
            notes_parts.append(f"canal: {canal}")
        if strategie:
            notes_parts.append(f"strategie: {strategie}")
        if derniere_action:
            notes_parts.append(f"derniere_action: {derniere_action}")
        if reponse:
            notes_parts.append(f"reponse: {reponse}")
        if notes_raw:
            notes_parts.append(notes_raw)
        notes_parts.append("source:notion_crm")
        combined_notes = " | ".join(notes_parts) or None

        if job_url:
            effective_url = job_url
        else:
            effective_url = f"https://notion.so/{page_id.replace('-', '')}"

        try:
            opp = OpportunityIn(
                source="notion_crm",
                title=position,
                company=company or "Unknown",
                url=effective_url,
                external_id=page_id,
                location=None,
                description=None,
                recruiter=recruiter_role,
                recruiter_linkedin_url=recruiter_linkedin,
                professional_email=None,
                notes=combined_notes,
            )
        except Exception as exc:
            return NotionImportRecord(
                page_data=page_data,
                opportunity=None,
                skip_reason=f"validation_error: {exc}",
            )

        return NotionImportRecord(
            page_data=page_data,
            opportunity=opp,
            skip_reason=None,
            mapped_status=map_notion_statut(notion_statut),
            mapped_contact_date=date_contact,
            mapped_last_contact=date_dernier_message,
            mapped_next_follow_up=prochaine_relance,
            mapped_follow_up_count=tentatives,
        )

    def parse_all_pages(self, raw_pages: list[dict]) -> list[NotionImportRecord]:
        """Parse all raw pages; per-page errors become skip records."""
        records: list[NotionImportRecord] = []
        for page in raw_pages:
            try:
                records.append(self.parse_page(page))
            except Exception as exc:
                pid = page.get("id", "?")
                logger.warning("Notion: failed to parse page %s: %s", pid, exc)
                records.append(
                    NotionImportRecord(
                        page_data=NotionPageData(
                            notion_page_id=pid,
                            notion_title="",
                            company="",
                            position="",
                            job_url=None,
                            recruiter_linkedin_url=None,
                            recruiter_role=None,
                            tags=[],
                            notion_type=None,
                            notion_statut=None,
                            canal=None,
                            strategie=None,
                            cv_utilise=None,
                            date_contact=None,
                            date_dernier_message=None,
                            prochaine_relance=None,
                            derniere_action=None,
                            reponse=None,
                            notes=None,
                            tentatives_messages=None,
                        ),
                        opportunity=None,
                        skip_reason=f"parse_exception: {exc}",
                    )
                )
        return records


def build_notion_provider(settings) -> NotionCRMProvider | None:
    """Build a NotionCRMProvider from settings, or return None when not configured."""
    if not settings.notion_api_key or not settings.notion_database_id:
        return None
    return NotionCRMProvider(
        api_key=settings.notion_api_key,
        database_id=settings.notion_database_id,
        batch_size=settings.notion_sync_batch_size,
    )
