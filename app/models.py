from datetime import date, datetime
from enum import Enum

from sqlalchemy import JSON, Date, DateTime, Enum as SqlEnum, Float, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class ReviewStatus(str, Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"


class MessageStatus(str, Enum):
    draft = "draft"
    approved = "approved"
    rejected = "rejected"
    sent = "sent"
    failed = "failed"


class MessageChannel(str, Enum):
    email = "email"
    linkedin = "linkedin"


class ApplicationStatus(str, Enum):
    discovered = "discovered"
    qualified = "qualified"
    researched = "researched"
    contact_ready = "contact_ready"
    contacted = "contacted"
    applied = "applied"
    waiting_response = "waiting_response"
    follow_up_due = "follow_up_due"
    responded = "responded"
    interview = "interview"
    offer = "offer"
    rejected = "rejected"
    withdrawn = "withdrawn"
    closed = "closed"


class ResponseStatus(str, Enum):
    none = "no response"
    positive = "positive response"
    negative = "negative response"
    neutral = "neutral response"
    interview = "interview invitation"
    info = "request for information"
    other = "other"


class DiscoveryRunStatus(str, Enum):
    running = "RUNNING"
    completed = "COMPLETED"
    partial = "PARTIAL"
    failed = "FAILED"
    skipped = "SKIPPED"


class DiscoveryRun(Base):
    __tablename__ = "discovery_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[DiscoveryRunStatus] = mapped_column(SqlEnum(DiscoveryRunStatus), default=DiscoveryRunStatus.running)
    sources_attempted: Mapped[int] = mapped_column(Integer, default=0)
    sources_succeeded: Mapped[int] = mapped_column(Integer, default=0)
    sources_failed: Mapped[int] = mapped_column(Integer, default=0)
    jobs_fetched: Mapped[int] = mapped_column(Integer, default=0)
    jobs_normalized: Mapped[int] = mapped_column(Integer, default=0)
    new_opportunities: Mapped[int] = mapped_column(Integer, default=0)
    new_high_value_opportunities: Mapped[int] = mapped_column(Integer, default=0)
    duplicates_ignored: Mapped[int] = mapped_column(Integer, default=0)
    rejected_low_score: Mapped[int] = mapped_column(Integer, default=0)
    errors_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    provider_metrics_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Application(Base):
    __tablename__ = "applications"
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_application_source_external_id"),
        UniqueConstraint("source", "job_url", name="uq_application_source_job_url"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"), nullable=True, index=True)
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    company: Mapped[str] = mapped_column(String(255), index=True)
    position: Mapped[str] = mapped_column(String(255), index=True)
    source: Mapped[str] = mapped_column(String(80), index=True)
    job_url: Mapped[str] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    recruiter: Mapped[str | None] = mapped_column(String(255), nullable=True)
    recruiter_linkedin_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    professional_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    discovery_date: Mapped[date] = mapped_column(Date, default=date.today)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    contact_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    application_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    channel: Mapped[str | None] = mapped_column(String(80), nullable=True)
    application_status: Mapped[ApplicationStatus] = mapped_column(
        SqlEnum(ApplicationStatus), default=ApplicationStatus.discovered
    )
    response_status: Mapped[ResponseStatus | None] = mapped_column(SqlEnum(ResponseStatus), nullable=True)
    last_contact: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_response_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    next_follow_up: Mapped[date | None] = mapped_column(Date, nullable=True)
    follow_up_count: Mapped[int] = mapped_column(Integer, default=0)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    application_method: Mapped[str | None] = mapped_column(String(80), nullable=True)
    external_application_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    application_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    score: Mapped[int] = mapped_column(Integer, default=0)
    score_reason: Mapped[str] = mapped_column(Text, default="")
    relevance_category: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    review_status: Mapped[ReviewStatus] = mapped_column(SqlEnum(ReviewStatus), default=ReviewStatus.pending)
    # Notion CRM integration — stores the Notion page ID for imported/matched records.
    # Nullable: existing records are unaffected. Added via ensure_additive_columns().
    notion_page_id: Mapped[str | None] = mapped_column(String(100), nullable=True, unique=True, index=True)
    generated_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    events: Mapped[list["ApplicationEvent"]] = relationship(
        back_populates="application", cascade="all, delete-orphan"
    )
    messages: Mapped[list["OutboundMessage"]] = relationship(
        back_populates="application", cascade="all, delete-orphan"
    )
    responses: Mapped[list["IncomingResponse"]] = relationship(
        back_populates="application", cascade="all, delete-orphan"
    )
    company_ref: Mapped["Company | None"] = relationship(back_populates="applications")


class ApplicationEvent(Base):
    __tablename__ = "application_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(80), index=True)
    channel: Mapped[str | None] = mapped_column(String(80), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    application: Mapped[Application] = relationship(back_populates="events")


class EmailKind(str, Enum):
    verified_individual = "verified_professional_individual"
    unverified_individual = "unverified_professional_email"
    generic = "generic_company_email"
    none = "no_email_found"


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    normalized_name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    website: Mapped[str | None] = mapped_column(Text, nullable=True)
    linkedin_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(80), default="application")
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    applications: Mapped[list[Application]] = relationship(back_populates="company_ref")
    contacts: Mapped[list["Contact"]] = relationship(back_populates="company", cascade="all, delete-orphan")
    emails: Mapped[list["ProfessionalEmail"]] = relationship(back_populates="company", cascade="all, delete-orphan")


class Contact(Base):
    __tablename__ = "contacts"
    __table_args__ = (
        UniqueConstraint("company_id", "name", "job_title", name="uq_contact_company_name_title"),
        UniqueConstraint("company_id", "linkedin_url", name="uq_contact_company_linkedin"),
        UniqueConstraint("company_id", "professional_email", name="uq_contact_company_email"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    job_title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    linkedin_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    professional_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    email_verification_status: Mapped[str | None] = mapped_column(String(80), nullable=True)
    email_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(80))
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    discovered_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    company: Mapped[Company] = relationship(back_populates="contacts")


class ProfessionalEmail(Base):
    __tablename__ = "professional_emails"
    __table_args__ = (
        UniqueConstraint("company_id", "address", name="uq_email_company_address"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id"), nullable=True, index=True)
    address: Mapped[str] = mapped_column(String(255), index=True)
    kind: Mapped[EmailKind] = mapped_column(SqlEnum(EmailKind))
    provider: Mapped[str] = mapped_column(String(80))
    verification_status: Mapped[str | None] = mapped_column(String(80), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    discovered_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    company: Mapped[Company] = relationship(back_populates="emails")


class OutboundMessage(Base):
    __tablename__ = "outbound_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id"), index=True)
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id"), nullable=True, index=True)
    channel: Mapped[MessageChannel] = mapped_column(SqlEnum(MessageChannel))
    subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    body: Mapped[str] = mapped_column(Text)
    generation_provider: Mapped[str | None] = mapped_column(String(80), nullable=True)
    generation_model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    status: Mapped[MessageStatus] = mapped_column(SqlEnum(MessageStatus), default=MessageStatus.draft)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    application: Mapped[Application] = relationship(back_populates="messages")
    contact: Mapped["Contact | None"] = relationship()


class IncomingResponse(Base):
    """A reply received from a recruiter/company, classified and linked to an application."""
    __tablename__ = "incoming_responses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id"), index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime)
    sender: Mapped[str] = mapped_column(String(255))
    subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_preview: Mapped[str | None] = mapped_column(Text, nullable=True)  # first 1000 chars, never deleted
    classification: Mapped[ResponseStatus | None] = mapped_column(SqlEnum(ResponseStatus), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)  # 0-1
    source: Mapped[str] = mapped_column(String(80), default="manual")  # "imap" | "manual"
    confirmed: Mapped[bool] = mapped_column(Integer, default=0)  # human confirmed classification
    message_id_header: Mapped[str | None] = mapped_column(String(500), nullable=True, unique=True)  # dedup key
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    application: Mapped[Application] = relationship(back_populates="responses")
