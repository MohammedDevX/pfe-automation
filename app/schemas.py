from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from app.models import ApplicationStatus, EmailKind, MessageChannel, MessageStatus, ResponseStatus, ReviewStatus



class OpportunityIn(BaseModel):
    source: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=255)
    company: str = Field(min_length=1, max_length=255)
    url: HttpUrl
    external_id: str | None = None
    location: str | None = None
    description: str | None = None
    recruiter: str | None = None
    recruiter_linkedin_url: HttpUrl | None = None
    professional_email: str | None = None
    notes: str | None = None
    posted_at: datetime | None = None


class ApplicationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    company_id: int | None
    external_id: str | None
    company: str
    position: str
    source: str
    job_url: str
    location: str | None
    recruiter: str | None
    recruiter_linkedin_url: str | None
    professional_email: str | None
    discovery_date: date
    posted_at: datetime | None
    contact_date: date | None
    application_date: date | None
    channel: str | None
    application_status: ApplicationStatus
    response_status: ResponseStatus | None
    last_contact: date | None
    last_response_date: date | None
    next_follow_up: date | None
    follow_up_count: int
    notes: str | None
    score: int
    score_reason: str
    relevance_category: str | None = None
    review_status: ReviewStatus
    generated_message: str | None
    created_at: datetime
    updated_at: datetime


class ReviewUpdate(BaseModel):
    review_status: ReviewStatus
    notes: str | None = None
    generated_message: str | None = None


class ApplicationPatch(BaseModel):
    recruiter: str | None = None
    recruiter_linkedin_url: HttpUrl | None = None
    professional_email: str | None = None
    contact_date: date | None = None
    application_date: date | None = None
    channel: str | None = None
    application_status: ApplicationStatus | None = None
    response_status: str | None = None
    last_contact: date | None = None
    next_follow_up: date | None = None
    notes: str | None = None
    generated_message: str | None = None


class EventCreate(BaseModel):
    event_type: str = Field(min_length=1, max_length=80)
    channel: str | None = None
    notes: str | None = None


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    application_id: int
    event_type: str
    channel: str | None
    notes: str | None
    created_at: datetime


class IngestResult(BaseModel):
    created: int
    updated: int
    applications: list[ApplicationOut]


class SearchCriteria(BaseModel):
    keywords: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    technologies: list[str] = Field(default_factory=list)
    date_from: date | None = None
    date_to: date | None = None
    providers: list[str] = Field(default_factory=lambda: ["stagiaires_ma", "greenhouse", "lever", "remotive", "arbeitnow", "jobicy", "weworkremotely", "remoteok", "adzuna", "pfedaba", "web_search"])
    results_per_page: int = Field(default=20, ge=1, le=50)
    max_pages: int = Field(default=1, ge=1, le=5)
    lever_sites: list[str] = Field(default_factory=lambda: ["blablacar", "scaleway", "malt", "brevo", "contentsquare"])
    greenhouse_boards: list[str] = Field(default_factory=lambda: ["doctolib", "canonical", "datadog", "algolia", "gitlab"])
    min_score: int = Field(default=35, ge=0, le=100)
    web_search_max_queries: int = Field(default=30, ge=1, le=100)
    web_search_max_results_per_query: int = Field(default=10, ge=1, le=50)
    web_search_max_pages_per_query: int = Field(default=2, ge=1, le=10)
    web_search_max_candidate_pages: int = Field(default=50, ge=1, le=200)


class ProviderStats(BaseModel):
    provider_name: str
    countries_covered: list[str] = Field(default_factory=list)
    access_method: str  # "API / structured public access", "public page access", "manual-only", "blocked/restricted"
    status: str         # "active", "credentials_missing", "blocked/restricted", "manual-only", "error"
    jobs_fetched: int = 0
    jobs_normalized: int = 0
    new_jobs: int = 0
    duplicates: int = 0
    low_score_jobs: int = 0
    errors_or_restrictions: str | None = None


class DiscoveryResult(BaseModel):
    run_id: str | None = None
    run_status: str = "COMPLETED"
    sources_queried: list[str]
    jobs_fetched: int
    jobs_normalized: int
    new_opportunities: int
    new_high_value_opportunities: int = 0
    duplicates_ignored: int
    rejected_low_score: int
    errors_by_provider: dict[str, str]
    morocco_count: int
    france_count: int
    canada_count: int = 0
    belgium_count: int = 0
    switzerland_count: int = 0
    remote_count: int
    other_countries_count: int = 0
    provider_stats: list[ProviderStats] = Field(default_factory=list)
    new_application_ids: set[int] = Field(default_factory=set)
    applications: list[ApplicationOut]


class DiscoveryRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    run_id: str
    started_at: datetime
    finished_at: datetime | None = None
    status: str
    sources_attempted: int = 0
    sources_succeeded: int = 0
    sources_failed: int = 0
    jobs_fetched: int = 0
    jobs_normalized: int = 0
    new_opportunities: int = 0
    new_high_value_opportunities: int = 0
    duplicates_ignored: int = 0
    rejected_low_score: int = 0
    errors_json: dict | None = None
    provider_metrics_json: list | None = None
    created_at: datetime


class CompanyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    normalized_name: str
    website: str | None
    linkedin_url: str | None
    location: str | None
    description: str | None
    source: str
    metadata_json: dict | None
    created_at: datetime
    updated_at: datetime


class ContactOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    company_id: int
    name: str
    job_title: str | None
    linkedin_url: str | None
    professional_email: str | None
    email_verification_status: str | None
    email_confidence: float | None
    source: str
    source_url: str | None
    discovered_at: datetime
    relevance: str = "LOW"
    relevance_reason: str = ""

    @model_validator(mode="after")
    def compute_relevance(self) -> "ContactOut":
        if self.relevance == "LOW" and not self.relevance_reason:
            from app.services.research import classify_contact_relevance
            rel, reason = classify_contact_relevance(self.job_title)
            self.relevance = rel
            self.relevance_reason = reason
        return self


class ProfessionalEmailOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    company_id: int
    contact_id: int | None
    address: str
    kind: EmailKind
    provider: str
    verification_status: str | None
    confidence: float | None
    source_url: str | None
    discovered_at: datetime


class ResearchRequest(BaseModel):
    providers: list[str] = Field(default_factory=lambda: ["public_website", "web_search", "web_search_recruiter"])
    website: HttpUrl | None = None
    include_web_search: bool = True
    include_hunter: bool = False




class ResearchResult(BaseModel):
    company: CompanyOut
    recruiters: list[ContactOut]
    professional_emails: list[ProfessionalEmailOut]
    provider_errors: list[str] = Field(default_factory=list)


class MessageGenerateRequest(BaseModel):
    channel: MessageChannel = MessageChannel.email
    contact_id: int | None = None
    provider: str = Field(default="template", description="'openai' or 'template'")
    language: str | None = Field(default=None, description="Message language ('fr' or 'en'). Defaults to auto-detect or default settings.")
    regenerate: bool = False


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    application_id: int
    contact_id: int | None
    channel: MessageChannel
    subject: str | None
    body: str
    generation_provider: str | None
    generation_model: str | None
    status: MessageStatus
    metadata_json: dict | None = None
    sent_message_id: str | None = None
    in_reply_to: str | None = None
    references_header: str | None = None
    created_at: datetime
    approved_at: datetime | None
    sent_at: datetime | None
    failure_reason: str | None



class MessagePatch(BaseModel):
    subject: str | None = None
    body: str | None = None


class MessageSendRequest(BaseModel):
    to_email: str | None = Field(
        default=None,
        description="Override recipient email. Required if no contact email was discovered.",
    )


class StatusTransitionRequest(BaseModel):
    status: ApplicationStatus
    notes: str | None = None


class RecordResponseRequest(BaseModel):
    received_at: datetime | None = None
    sender: str
    subject: str | None = None
    body_preview: str | None = None
    classification: ResponseStatus | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    source: str = "manual"
    confirmed: bool = False
    message_id_header: str | None = None
    in_reply_to_header: str | None = None


class IncomingResponseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    application_id: int
    received_at: datetime
    sender: str
    subject: str | None
    body_preview: str | None
    classification: ResponseStatus | None
    confidence: float | None
    source: str
    confirmed: bool
    message_id_header: str | None = None
    in_reply_to_header: str | None = None
    created_at: datetime


class ConfirmResponseRequest(BaseModel):
    classification: ResponseStatus


class ReassignResponseRequest(BaseModel):
    application_id: int



class FollowUpDueItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    company: str
    position: str
    next_follow_up: date | None
    follow_up_count: int
    application_status: ApplicationStatus
    last_contact: date | None


class ApplicationQuestionOut(BaseModel):
    id: str
    label: str
    required: bool
    type: str
    options: list[str] | None = None


class ApplicationPayloadOut(BaseModel):
    provider_name: str
    is_ready: bool
    missing_fields: list[str]
    questions: list[ApplicationQuestionOut]
    answers: dict[str, Any]
    candidate: dict[str, Any]


class ApplicationSubmitRequest(BaseModel):
    answers_override: dict[str, Any] | None = None
    force_resubmit: bool = False


class NotionSyncResult(BaseModel):
    """Result of a Notion -> SQLite import operation."""
    dry_run: bool
    pages_read: int = 0
    new_imported: int = 0
    matched_existing: int = 0
    skipped_empty: int = 0
    errors: list[str] = Field(default_factory=list)
    preview: list[dict] = Field(default_factory=list)


class NotionConnectionStatus(BaseModel):
    """Public status of the Notion integration. Never exposes secrets."""
    configured: bool
    # Last 8 chars of database_id only — enough to confirm the right DB without exposing it.
    database_id_suffix: str | None = None
    last_sync_at: datetime | None = None
    last_sync_result: str | None = None
    error: str | None = None
