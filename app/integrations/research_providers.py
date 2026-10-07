from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
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

CAREERS_PATH_PATTERNS = [
    re.compile(r'href=["\']([^"\']*(?:careers|jobs|recrutement|career|offres-emploi|join-us|emplois|we-are-hiring)[^"\']*)["\']', re.IGNORECASE),
]

AGGREGATOR_DOMAINS = frozenset({
    "indeed.com", "indeed.fr", "glassdoor.com", "glassdoor.fr", "linkedin.com",
    "welcometothejungle.com", "hellowork.com", "monster.com", "monster.fr",
    "talent.com", "stepstone.de", "rekrute.com", "emploidakar.com",
    "marocannonces.com", "stagiaires.ma", "jobteaser.com", "cadremploi.fr",
    "apec.fr", "wikipedia.org", "facebook.com", "twitter.com", "x.com",
    "instagram.com", "youtube.com", "crunchbase.com", "pagesjaunes.fr",
    "societe.com", "verif.com", "kompass.com", "yellowpages.com",
})

TECH_SIGNAL_PATTERNS = [
    (re.compile(r"(?:\.net\s+core|\.net\b|\bdotnet\b)", re.IGNORECASE), ".NET"),
    (re.compile(r"(?:c#|\bc-sharp\b)", re.IGNORECASE), "C#"),
    (re.compile(r"\basp\.net(?:\s+core)?\b", re.IGNORECASE), "ASP.NET Core"),
    (re.compile(r"\bspring\s*boot\b", re.IGNORECASE), "Spring Boot"),
    (re.compile(r"\bjava\b", re.IGNORECASE), "Java"),
    (re.compile(r"\bpython\b", re.IGNORECASE), "Python"),
    (re.compile(r"\bfastapi\b", re.IGNORECASE), "FastAPI"),
    (re.compile(r"\bdjango\b", re.IGNORECASE), "Django"),
    (re.compile(r"\bflask\b", re.IGNORECASE), "Flask"),
    (re.compile(r"\bnode(?:\.js)?\b", re.IGNORECASE), "Node.js"),
    (re.compile(r"\btypescript\b", re.IGNORECASE), "TypeScript"),
    (re.compile(r"\bjavascript\b", re.IGNORECASE), "JavaScript"),
    (re.compile(r"\breact(?:\.js)?\b", re.IGNORECASE), "React"),
    (re.compile(r"\bangular(?:\.js)?\b", re.IGNORECASE), "Angular"),
    (re.compile(r"\bvue(?:\.js)?\b", re.IGNORECASE), "Vue.js"),
    (re.compile(r"\bnext(?:\.js)?\b", re.IGNORECASE), "Next.js"),
    (re.compile(r"\bphp\b", re.IGNORECASE), "PHP"),
    (re.compile(r"\bsymfony\b", re.IGNORECASE), "Symfony"),
    (re.compile(r"\blaravel\b", re.IGNORECASE), "Laravel"),
    (re.compile(r"\bgolang\b|\bgo\s+language\b", re.IGNORECASE), "Go"),
    (re.compile(r"\brust\b", re.IGNORECASE), "Rust"),
    (re.compile(r"\bpostgresql\b|\bpostgres\b", re.IGNORECASE), "PostgreSQL"),
    (re.compile(r"\bmysql\b", re.IGNORECASE), "MySQL"),
    (re.compile(r"\bmongodb\b", re.IGNORECASE), "MongoDB"),
    (re.compile(r"\bredis\b", re.IGNORECASE), "Redis"),
    (re.compile(r"\bsql\b", re.IGNORECASE), "SQL"),
    (re.compile(r"\bdocker\b", re.IGNORECASE), "Docker"),
    (re.compile(r"\bkubernetes\b|\bk8s\b", re.IGNORECASE), "Kubernetes"),
    (re.compile(r"\bdevops\b", re.IGNORECASE), "DevOps"),
    (re.compile(r"\bci/cd\b", re.IGNORECASE), "CI/CD"),
    (re.compile(r"\baws\b|\bamazon\s+web\s+services\b", re.IGNORECASE), "AWS"),
    (re.compile(r"\bazure\b", re.IGNORECASE), "Azure"),
    (re.compile(r"\bgcp\b|\bgoogle\s+cloud\b", re.IGNORECASE), "GCP"),
    (re.compile(r"\bkafka\b", re.IGNORECASE), "Kafka"),
    (re.compile(r"\bgraphql\b", re.IGNORECASE), "GraphQL"),
    (re.compile(r"\bqa\b|\bquality\s+assurance\b|\btest\s+automation\b", re.IGNORECASE), "QA / Testing"),
    (re.compile(r"\bselenium\b", re.IGNORECASE), "Selenium"),
    (re.compile(r"\bcypress\b", re.IGNORECASE), "Cypress"),
    (re.compile(r"\bplaywright\b", re.IGNORECASE), "Playwright"),
    (re.compile(r"\bpytorch\b", re.IGNORECASE), "PyTorch"),
    (re.compile(r"\btensorflow\b", re.IGNORECASE), "TensorFlow"),
    (re.compile(r"\bmachine\s+learning\b|\bdeep\s+learning\b", re.IGNORECASE), "Machine Learning"),
    (re.compile(r"\bdata\s+engineering\b", re.IGNORECASE), "Data Engineering"),
]


def extract_technology_signals(text: str) -> list[str]:
    """Extract recognized technology names from text deterministically."""
    if not text:
        return []
    signals: list[str] = []
    seen: set[str] = set()
    for pattern, name in TECH_SIGNAL_PATTERNS:
        if pattern.search(text) and name not in seen:
            seen.add(name)
            signals.append(name)
    return signals


@dataclass
class ProviderResearchResult:
    website: str | None = None
    careers_url: str | None = None
    linkedin_url: str | None = None
    location: str | None = None
    description: str | None = None
    industry: str | None = None
    technology_signals: list[str] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)
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

                # 1. Provenance
                result.sources.append({
                    "type": "official_website",
                    "url": url,
                    "confidence": "high",
                })

                # 2. LinkedIn company page
                if not result.linkedin_url:
                    result.linkedin_url = _first_linkedin_company_url(text)
                    if result.linkedin_url:
                        result.sources.append({
                            "type": "linkedin_company",
                            "url": result.linkedin_url,
                            "confidence": "high",
                        })

                # 3. Careers URL from links
                if not result.careers_url:
                    careers_link = _find_careers_link(company.website, text)
                    if careers_link:
                        result.careers_url = careers_link
                        result.sources.append({
                            "type": "careers_page",
                            "url": careers_link,
                            "confidence": "high",
                        })

                # 4. Meta description
                if not result.description:
                    meta_desc = _extract_meta_description(text)
                    if meta_desc:
                        result.description = meta_desc

                # 5. Technology signals
                found_tech = extract_technology_signals(text)
                for tech in found_tech:
                    if tech not in result.technology_signals:
                        result.technology_signals.append(tech)

                # 6. Emails
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


class WebSearchCompanyProvider(ResearchProvider):
    """Discovers official company website, LinkedIn company page, and careers URL using web search."""
    name = "web_search"

    def __init__(self, settings: Settings):
        self.settings = settings

    async def research(self, company: Company, application: Application) -> ProviderResearchResult:
        from app.integrations.web_search import build_search_engine
        engine = build_search_engine(self.settings)
        if not engine:
            return ProviderResearchResult(metadata={"web_search": "search engine unconfigured"})

        result = ProviderResearchResult(metadata={"provider": self.name})
        company_name = company.name.strip()

        # 1. Search for official website if not already present
        if not company.website and not result.website:
            try:
                search_results = await engine.search(f'"{company_name}" official website', limit=5)
                for item in search_results:
                    url = item.url
                    domain = _domain(url)
                    if domain and not any(agg in domain for agg in AGGREGATOR_DOMAINS):
                        parsed = urlparse(url)
                        clean_website = f"{parsed.scheme}://{parsed.netloc}"
                        result.website = clean_website
                        result.sources.append({
                            "type": "web_search",
                            "url": clean_website,
                            "confidence": "medium",
                        })
                        break
            except Exception as exc:
                result.metadata["website_search_error"] = str(exc)

        # 2. Search for LinkedIn company page if not present
        if not company.linkedin_url and not result.linkedin_url:
            try:
                li_results = await engine.search(f'site:linkedin.com/company "{company_name}"', limit=3)
                for item in li_results:
                    li_url = _first_linkedin_company_url(item.url)
                    if li_url:
                        result.linkedin_url = li_url
                        result.sources.append({
                            "type": "linkedin_company",
                            "url": li_url,
                            "confidence": "medium",
                        })
                        break
            except Exception as exc:
                result.metadata["linkedin_search_error"] = str(exc)

        # 3. Search for careers page if not present
        target_site = result.website or company.website
        if target_site and not result.careers_url:
            target_domain = _domain(target_site)
            try:
                careers_results = await engine.search(f'site:{target_domain} careers OR jobs OR recrutement', limit=3)
                for item in careers_results:
                    item_domain = _domain(item.url)
                    if item_domain == target_domain:
                        result.careers_url = item.url
                        result.sources.append({
                            "type": "careers_page",
                            "url": item.url,
                            "confidence": "medium",
                        })
                        break
            except Exception as exc:
                result.metadata["careers_search_error"] = str(exc)

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


class WebSearchRecruiterProvider(ResearchProvider):
    """Discovers recruiter and HR contacts using targeted web searches."""

    name = "web_search_recruiter"

    def __init__(self, settings: Settings):
        self.settings = settings

    async def research(self, company: Company, application: Application) -> ProviderResearchResult:
        from app.integrations.web_search import build_search_engine

        engine = build_search_engine(self.settings)
        if not engine:
            return ProviderResearchResult(metadata={"web_search_recruiter": "search engine unconfigured"})

        result = ProviderResearchResult(metadata={"provider": self.name})
        company_name = company.name.strip()
        company_domain = _domain(company.website) if company.website else None

        queries = [
            f'site:linkedin.com/in "Talent Acquisition" "{company_name}"',
            f'site:linkedin.com/in "Technical Recruiter" "{company_name}"',
            f'site:linkedin.com/in "Recruiter" "{company_name}"',
            f'site:linkedin.com/in "HR" "{company_name}"',
            f'site:linkedin.com/in "Campus Recruiter" "{company_name}"',
            f'site:linkedin.com/in "Engineering Manager" "{company_name}"',
            f'site:linkedin.com/in "Head of Engineering" "{company_name}"',
        ]
        if company_domain:
            queries.append(f'"{company_name}" recruiter OR "campus recruiter" contact email')

        for query in queries:
            try:
                search_results = await engine.search(query, limit=5)
                for item in search_results:
                    parsed_contacts, parsed_emails = _parse_recruiter_search_item(item, company_name, company_domain)
                    for candidate in parsed_contacts:
                        if not any(
                            (c.linkedin_url and c.linkedin_url == candidate.linkedin_url)
                            or (c.name.lower() == candidate.name.lower() and (c.job_title or "").lower() == (candidate.job_title or "").lower())
                            for c in result.contacts
                        ):
                            result.contacts.append(candidate)
                    for candidate in parsed_emails:
                        if not any(e.address.lower() == candidate.address.lower() for e in result.emails):
                            result.emails.append(candidate)
            except Exception as exc:
                result.metadata[f"search_error_{query[:20]}"] = str(exc)

        return result


def build_research_providers(request: ResearchRequest, settings: Settings) -> list[ResearchProvider]:
    requested = set(request.providers)
    if request.include_hunter:
        requested.add("hunter")
    providers: list[ResearchProvider] = []
    if "public_website" in requested:
        providers.append(PublicWebsiteProvider())
    if "web_search" in requested or getattr(request, "include_web_search", False):
        providers.append(WebSearchCompanyProvider(settings))
    if "web_search_recruiter" in requested or "web_search" in requested or getattr(request, "include_web_search", False):
        providers.append(WebSearchRecruiterProvider(settings))
    if "hunter" in requested:
        providers.append(HunterProvider(settings))
    return providers



def _domain(url: str) -> str:
    parsed = urlparse(url)
    return parsed.netloc.lower().removeprefix("www.")


def _first_linkedin_company_url(text: str) -> str | None:
    match = re.search(r"https?://(?:www\.)?linkedin\.com/company/[A-Za-z0-9_.%-]+/?", text)
    if match:
        url = match.group(0).rstrip("/")
        return url
    return None


def _find_careers_link(base_url: str, html: str) -> str | None:
    for pattern in CAREERS_PATH_PATTERNS:
        match = pattern.search(html)
        if match:
            rel = match.group(1).strip()
            if rel.startswith("http://") or rel.startswith("https://"):
                return rel
            return urljoin(base_url.rstrip("/") + "/", rel.lstrip("/"))
    return None


def _extract_meta_description(html: str) -> str | None:
    match = (
        re.search(r'<meta[^>]*property=["\']og:description["\'][^>]*content=["\']([^"\']+)["\']', html, re.I)
        or re.search(r'<meta[^>]*name=["\']description["\'][^>]*content=["\']([^"\']+)["\']', html, re.I)
    )
    if match:
        desc = match.group(1).strip()
        if len(desc) >= 10:
            return desc
    return None


def _is_recruiting_role(title: str) -> bool:
    lowered = title.lower()
    return any(term in lowered for term in RECRUITER_TERMS)


LINKEDIN_PROFILE_RE = re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/([A-Za-z0-9_-]+)/?", re.I)
INVALID_NAME_PATTERNS = re.compile(
    r"\b(?:"
    r"jobs|careers|career|offres|stage|internship|intern|recruitment|recrutement|recruiter|recruiters|recruiting|"
    r"talent|acquisition|human|resources|hr|hrbp|hiring|manager|managers|head|lead|director|vp|cto|officer|"
    r"engineer|developer|architect|analyst|specialist|consultant|partner|team|department|group|division|"
    r"company|corp|corporation|inc|ltd|sarl|sas|llc|gmbh|co|associates|agency|solutions|services|"
    r"linkedin|profile|profiles|view|top|best|find|hire|search|list|directory|overview|about|contact|us|"
    r"france|paris|london|york|usa|world|worldwide|remote|location|site|blog|news|article|forum|working"
    r")\b",
    re.IGNORECASE,
)



def _parse_recruiter_search_item(item, company_name: str, domain: str | None = None) -> tuple[list[ContactCandidate], list[EmailCandidate]]:
    contacts: list[ContactCandidate] = []
    emails: list[EmailCandidate] = []

    # 1. Extract emails from snippet
    if getattr(item, "snippet", None):
        found_emails = EMAIL_RE.findall(item.snippet)
        for addr in set(found_emails):
            kind = classify_email(addr, domain)
            if kind.value != "no_email_found":
                emails.append(
                    EmailCandidate(
                        address=addr.lower(),
                        kind=kind,
                        provider="web_search_recruiter",
                        verification_status="unverified",
                        confidence=0.6 if domain and domain in addr.lower() else 0.4,
                        source_url=item.url,
                    )
                )

    # 2. Extract LinkedIn URL if profile link
    match = LINKEDIN_PROFILE_RE.search(item.url)
    clean_linkedin_url = f"https://www.linkedin.com/in/{match.group(1)}" if match else None

    # 3. Parse Name and Title from title string or snippet
    title_text = getattr(item, "title", "") or ""
    title_parts = [p.strip() for p in re.split(r"[-|:]", title_text) if p.strip()]

    name: str | None = None
    job_title: str | None = None

    if title_parts:
        first_part = title_parts[0]
        words = first_part.split()
        if 2 <= len(words) <= 4 and not INVALID_NAME_PATTERNS.search(first_part):
            name = first_part

        if len(title_parts) >= 2:
            second_part = title_parts[1]
            if "linkedin" not in second_part.lower() and len(second_part) <= 100:
                job_title = second_part


    if not job_title and getattr(item, "snippet", None):
        for term in RECRUITER_TERMS:
            if term in item.snippet.lower():
                m = re.search(r"([^.,;]*\b" + re.escape(term) + r"\b[^.,;]*)", item.snippet, re.I)
                if m:
                    candidate_title = m.group(1).strip()
                    if len(candidate_title) <= 60:
                        job_title = candidate_title
                        break

    if name:
        prof_email = emails[0].address if emails else None
        contacts.append(
            ContactCandidate(
                name=name,
                job_title=job_title,
                linkedin_url=clean_linkedin_url,
                professional_email=prof_email,
                email_verification_status="unverified" if prof_email else None,
                email_confidence=emails[0].confidence if emails else None,
                source="web_search_recruiter",
                source_url=item.url,
            )
        )

    return contacts, emails


