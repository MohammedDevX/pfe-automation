from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import re
from urllib.parse import urljoin, urlparse

import httpx

from app.database import Settings
from app.models import Application, Company
from app.schemas import ResearchRequest
from app.services.research import (
    ContactCandidate,
    EmailCandidate,
    RECRUITER_TERMS,
    classify_email,
)


EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)


@dataclass
class ProviderResearchResult:
    website: str | None = None
    linkedin_url: str | None = None
    location: str | None = None
    description: str | None = None
    metadata: dict = field(default_factory=dict)
    contacts: list[ContactCandidate] = field(default_factory=list)
    emails: list[EmailCandidate] = field(default_factory=list)


class ResearchProvider(ABC):
    name: str

    @abstractmethod
    async def research(self, company: Company, application: Application) -> ProviderResearchResult:
        raise NotImplementedError


class PublicWebsiteProvider(ResearchProvider):
    name = "public_website"

    async def research(self, company: Company, application: Application) -> ProviderResearchResult:
        if not company.website:
            return ProviderResearchResult(metadata={"public_website": "no company website available"})

        urls = [company.website, urljoin(company.website.rstrip("/") + "/", "contact")]
        result = ProviderResearchResult(website=company.website, metadata={"provider": self.name})
        domain = _domain(company.website)

        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            for url in urls:
                try:
                    response = await client.get(url, headers={"User-Agent": "pfe-job-research/0.1"})
                    response.raise_for_status()
                except httpx.HTTPError:
                    continue

                text = response.text
                result.linkedin_url = result.linkedin_url or _first_linkedin_company_url(text)
                for address in sorted(set(EMAIL_RE.findall(text))):
                    kind = classify_email(address, domain)
                    if kind.value == "no_email_found":
                        continue
                    result.emails.append(
                        EmailCandidate(
                            address=address,
                            kind=kind,
                            provider=self.name,
                            verification_status="unverified",
                            confidence=0.55 if "contact" in url else 0.45,
                            source_url=url,
                        )
                    )

        return result


class HunterProvider(ResearchProvider):
    name = "hunter"

    def __init__(self, settings: Settings):
        self.settings = settings

    async def research(self, company: Company, application: Application) -> ProviderResearchResult:
        if not self.settings.hunter_api_key:
            raise RuntimeError("Hunter requires HUNTER_API_KEY.")
        if not company.website:
            raise RuntimeError("Hunter requires a company website/domain.")

        domain = _domain(company.website)
        result = ProviderResearchResult(website=company.website, metadata={"provider": self.name})
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                "https://api.hunter.io/v2/domain-search",
                params={
                    "domain": domain,
                    "api_key": self.settings.hunter_api_key,
                    "department": "hr",
                    "limit": 10,
                },
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
        data = response.json().get("data", {})
        organization = data.get("organization")
        if organization and not result.description:
            result.description = f"Hunter organization: {organization}"
        result.metadata["hunter_pattern"] = data.get("pattern")

        for item in data.get("emails", []):
            email = item.get("value")
            if not email:
                continue
            title = item.get("position")
            if title and not _is_recruiting_role(title):
                continue
            verified = item.get("verification", {}).get("status") == "valid"
            kind = classify_email(email, domain, verified=verified)
            confidence = item.get("confidence")
            result.emails.append(
                EmailCandidate(
                    address=email,
                    kind=kind,
                    provider=self.name,
                    verification_status=item.get("verification", {}).get("status") or "unverified",
                    confidence=float(confidence) / 100 if confidence is not None else None,
                    source_url=f"https://hunter.io/search/{domain}",
                )
            )
            first_name = item.get("first_name")
            last_name = item.get("last_name")
            full_name = " ".join(part for part in (first_name, last_name) if part).strip()
            if full_name and title:
                result.contacts.append(
                    ContactCandidate(
                        name=full_name,
                        job_title=title,
                        linkedin_url=item.get("linkedin"),
                        professional_email=email,
                        email_verification_status=item.get("verification", {}).get("status") or "unverified",
                        email_confidence=float(confidence) / 100 if confidence is not None else None,
                        source=self.name,
                        source_url=f"https://hunter.io/search/{domain}",
                    )
                )

        return result


def build_research_providers(request: ResearchRequest, settings: Settings) -> list[ResearchProvider]:
    requested = set(request.providers)
    if request.include_hunter:
        requested.add("hunter")
    providers: list[ResearchProvider] = []
    if "public_website" in requested:
        providers.append(PublicWebsiteProvider())
    if "hunter" in requested:
        providers.append(HunterProvider(settings))
    return providers


def _domain(url: str) -> str:
    parsed = urlparse(url)
    return parsed.netloc.lower().removeprefix("www.")


def _first_linkedin_company_url(text: str) -> str | None:
    match = re.search(r"https?://(?:www\.)?linkedin\.com/company/[A-Za-z0-9_.%-]+/?", text)
    return match.group(0) if match else None


def _is_recruiting_role(title: str) -> bool:
    lowered = title.lower()
    return any(term in lowered for term in RECRUITER_TERMS)
