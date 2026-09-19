import re
from dataclasses import dataclass
from urllib.parse import urlparse

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.database import Settings
from app.models import Application, Company, Contact, EmailKind, ProfessionalEmail
from app.schemas import ResearchRequest, ResearchResult


RECRUITER_TERMS = (
    "recruiter",
    "talent acquisition",
    "human resources",
    "hr",
    "hiring manager",
    "technical recruiter",
    "talent",
)

GENERIC_PREFIXES = {
    "contact",
    "info",
    "jobs",
    "careers",
    "career",
    "recruitment",
    "recrutement",
    "rh",
    "hr",
}

FREE_EMAIL_DOMAINS = {
    "gmail.com",
    "yahoo.com",
    "hotmail.com",
    "outlook.com",
    "live.com",
    "icloud.com",
}


@dataclass(frozen=True)
class ContactCandidate:
    name: str
    job_title: str | None
    linkedin_url: str | None
    professional_email: str | None
    email_verification_status: str | None
    email_confidence: float | None
    source: str
    source_url: str | None


@dataclass(frozen=True)
class EmailCandidate:
    address: str
    kind: EmailKind
    provider: str
    verification_status: str | None
    confidence: float | None
    source_url: str | None


def normalize_company_name(name: str) -> str:
    normalized = name.lower().strip()
    normalized = re.sub(r"[^\w\s&.-]", " ", normalized)
    normalized = re.sub(r"\b(inc|llc|ltd|limited|sa|sarl|sas|corp|corporation|company|co)\b\.?", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip()


def classify_email(address: str, company_domain: str | None = None, verified: bool = False) -> EmailKind:
    local, _, domain = address.lower().partition("@")
    if not domain or domain in FREE_EMAIL_DOMAINS:
        return EmailKind.none
    if local in GENERIC_PREFIXES:
        return EmailKind.generic
    if company_domain and domain != company_domain.lower().removeprefix("www."):
        return EmailKind.none
    return EmailKind.verified_individual if verified else EmailKind.unverified_individual


async def research_application(
    db: Session,
    application: Application,
    request: ResearchRequest,
    settings: Settings,
) -> ResearchResult:
    company = get_or_create_company(db, application, str(request.website) if request.website else None)
    application.company_id = company.id
    db.flush()

    from app.integrations.research_providers import build_research_providers

    provider_errors: list[str] = []
    for provider in build_research_providers(request, settings):
        try:
            result = await provider.research(company, application)
        except RuntimeError as exc:
            provider_errors.append(str(exc))
            continue
        except Exception as exc:
            provider_errors.append(f"{provider.name} failed: {exc}")
            continue

        if result.website and not company.website:
            company.website = result.website
        if result.linkedin_url and not company.linkedin_url:
            company.linkedin_url = result.linkedin_url
        if result.description and not company.description:
            company.description = result.description
        if result.location and not company.location:
            company.location = result.location
        if result.metadata:
            company.metadata_json = {**(company.metadata_json or {}), **result.metadata}

        for candidate in result.contacts:
            upsert_contact(db, company, candidate)
        for candidate in result.emails:
            if candidate.kind != EmailKind.none:
                upsert_email(db, company, candidate)

    db.commit()
    db.refresh(company)
    return ResearchResult(
        company=company,
        recruiters=list(company.contacts),
        professional_emails=list(company.emails),
        provider_errors=provider_errors,
    )


def get_or_create_company(db: Session, application: Application, website: str | None = None) -> Company:
    if website and "notion.so" in website.lower():
        website = None
    normalized = normalize_company_name(application.company)
    company = db.scalar(select(Company).where(Company.normalized_name == normalized))
    if company:
        if website and not company.website:
            company.website = website
        return company

    inferred_website = website or infer_company_website(application.job_url)
    company = Company(
        name=application.company,
        normalized_name=normalized,
        website=inferred_website,
        location=application.location,
        source=application.source,
    )
    db.add(company)
    db.flush()
    return company


def infer_company_website(job_url: str) -> str | None:
    if not job_url:
        return None
    parsed = urlparse(job_url)
    domain = parsed.netloc.lower().removeprefix("www.")
    if not domain or any(blocked in domain for blocked in ("adzuna", "greenhouse", "lever.co", "notion.so")):
        return None
    return f"{parsed.scheme}://{domain}"


def upsert_contact(db: Session, company: Company, candidate: ContactCandidate) -> Contact:
    query = select(Contact).where(Contact.company_id == company.id)
    if candidate.linkedin_url:
        query = query.where(Contact.linkedin_url == candidate.linkedin_url)
    elif candidate.professional_email:
        query = query.where(func.lower(Contact.professional_email) == candidate.professional_email.lower())
    else:
        query = query.where(
            (func.lower(Contact.name) == candidate.name.lower())
            & (func.lower(func.coalesce(Contact.job_title, "")) == (candidate.job_title or "").lower())
        )
    contact = db.scalar(query)
    if not contact:
        contact = Contact(company_id=company.id, name=candidate.name, source=candidate.source)
        db.add(contact)
    contact.job_title = candidate.job_title or contact.job_title
    contact.linkedin_url = candidate.linkedin_url or contact.linkedin_url
    contact.professional_email = candidate.professional_email or contact.professional_email
    contact.email_verification_status = candidate.email_verification_status or contact.email_verification_status
    contact.email_confidence = candidate.email_confidence if candidate.email_confidence is not None else contact.email_confidence
    contact.source = candidate.source
    contact.source_url = candidate.source_url or contact.source_url
    db.flush()
    return contact


def upsert_email(db: Session, company: Company, candidate: EmailCandidate) -> ProfessionalEmail:
    email = db.scalar(
        select(ProfessionalEmail).where(
            (ProfessionalEmail.company_id == company.id)
            & (func.lower(ProfessionalEmail.address) == candidate.address.lower())
        )
    )
    if not email:
        email = ProfessionalEmail(
            company_id=company.id,
            address=candidate.address.lower(),
            kind=candidate.kind,
            provider=candidate.provider,
        )
        db.add(email)
    email.kind = candidate.kind
    email.provider = candidate.provider
    email.verification_status = candidate.verification_status
    email.confidence = candidate.confidence
    email.source_url = candidate.source_url
    db.flush()
    return email
