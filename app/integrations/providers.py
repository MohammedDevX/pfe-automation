from abc import ABC, abstractmethod
from datetime import datetime, timezone
import json
import re

import httpx

from app.database import Settings
from app.integrations.ats import clean_html, fetch_greenhouse, fetch_lever
from app.schemas import OpportunityIn, SearchCriteria


class DiscoveryProvider(ABC):
    name: str
    access_method: str = "API / structured public access"
    countries_covered: list[str] = []
    status: str = "active"
    restriction_reason: str | None = None

    @abstractmethod
    async def search(self, criteria: SearchCriteria) -> list[OpportunityIn]:
        raise NotImplementedError


# ===========================================================================
# Active Automated API Providers
# ===========================================================================

class GreenhouseProvider(DiscoveryProvider):
    name = "greenhouse"
    access_method = "API / structured public access"
    countries_covered = ["France", "Morocco", "Remote", "International"]
    status = "active"

    def __init__(self, boards: list[str]):
        self.boards = boards

    async def search(self, criteria: SearchCriteria) -> list[OpportunityIn]:
        opportunities: list[OpportunityIn] = []
        for board in self.boards:
            for opportunity in await fetch_greenhouse(board):
                if _matches_criteria(opportunity, criteria):
                    opportunities.append(opportunity)
        return opportunities


class LeverProvider(DiscoveryProvider):
    name = "lever"
    access_method = "API / structured public access"
    countries_covered = ["France", "International", "Remote"]
    status = "active"

    def __init__(self, sites: list[str]):
        self.sites = sites

    async def search(self, criteria: SearchCriteria) -> list[OpportunityIn]:
        opportunities: list[OpportunityIn] = []
        for site in self.sites:
            for opportunity in await fetch_lever(site):
                if _matches_criteria(opportunity, criteria):
                    opportunities.append(opportunity)
        return opportunities


class RemotiveProvider(DiscoveryProvider):
    name = "remotive"
    access_method = "API / structured public access"
    countries_covered = ["International", "Remote", "Worldwide"]
    status = "active"

    async def search(self, criteria: SearchCriteria) -> list[OpportunityIn]:
        url = "https://remotive.com/api/remote-jobs?category=software-dev&limit=50"
        opportunities: list[OpportunityIn] = []
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(url, headers={"Accept": "application/json"})
            response.raise_for_status()
            jobs = response.json().get("jobs", [])
            for item in jobs:
                opp = OpportunityIn(
                    source="remotive",
                    external_id=str(item.get("id")),
                    title=item.get("title") or "Untitled",
                    company=item.get("company_name") or "Unknown company",
                    url=item.get("url"),
                    location=item.get("candidate_required_location") or "Remote",
                    description=clean_html(item.get("description")),
                    posted_at=_parse_datetime(item.get("publication_date")),
                )
                if _matches_criteria(opp, criteria):
                    opportunities.append(opp)
        return opportunities


class ArbeitnowProvider(DiscoveryProvider):
    name = "arbeitnow"
    access_method = "API / structured public access"
    countries_covered = ["Germany", "Switzerland", "Netherlands", "UK", "Remote", "Europe"]
    status = "active"

    async def search(self, criteria: SearchCriteria) -> list[OpportunityIn]:
        url = "https://www.arbeitnow.com/api/job-board-api"
        opportunities: list[OpportunityIn] = []
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(url, headers={"Accept": "application/json"})
            response.raise_for_status()
            jobs = response.json().get("data", [])
            for item in jobs:
                loc = item.get("location") or "Europe"
                if item.get("remote"):
                    loc = f"{loc} (Remote)"
                dt = None
                if item.get("created_at"):
                    try:
                        dt = datetime.fromtimestamp(item["created_at"], timezone.utc)
                    except Exception:
                        pass
                opp = OpportunityIn(
                    source="arbeitnow",
                    external_id=item.get("slug") or str(item.get("created_at")),
                    title=item.get("title") or "Untitled",
                    company=item.get("company_name") or "Unknown company",
                    url=item.get("url"),
                    location=loc,
                    description=clean_html(item.get("description")),
                    posted_at=dt,
                )
                if _matches_criteria(opp, criteria):
                    opportunities.append(opp)
        return opportunities


class JobicyProvider(DiscoveryProvider):
    name = "jobicy"
    access_method = "API / structured public access"
    countries_covered = ["International", "Remote", "Europe", "UK", "Worldwide"]
    status = "active"

    async def search(self, criteria: SearchCriteria) -> list[OpportunityIn]:
        url = "https://jobicy.com/api/v2/remote-jobs?count=30&tag=dev"
        opportunities: list[OpportunityIn] = []
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(url, headers={"Accept": "application/json"})
            response.raise_for_status()
            jobs = response.json().get("jobs", [])
            for item in jobs:
                opp = OpportunityIn(
                    source="jobicy",
                    external_id=str(item.get("id")),
                    title=item.get("jobTitle") or "Untitled",
                    company=item.get("companyName") or "Unknown company",
                    url=item.get("url"),
                    location=item.get("jobGeo") or "Remote",
                    description=clean_html(item.get("jobDescription")),
                    posted_at=_parse_datetime(item.get("pubDate")),
                )
                if _matches_criteria(opp, criteria):
                    opportunities.append(opp)
        return opportunities


class AdzunaProvider(DiscoveryProvider):
    name = "adzuna"
    access_method = "API / structured public access"
    countries_covered = ["Morocco", "France", "UK", "Canada"]

    def __init__(self, settings: Settings):
        self.settings = settings
        if not settings.adzuna_app_id or not settings.adzuna_app_key:
            self.status = "credentials_missing"
            self.restriction_reason = "Adzuna requires ADZUNA_APP_ID and ADZUNA_APP_KEY."
        else:
            self.status = "active"

    async def search(self, criteria: SearchCriteria) -> list[OpportunityIn]:
        if not self.settings.adzuna_app_id or not self.settings.adzuna_app_key:
            raise RuntimeError("Adzuna requires ADZUNA_APP_ID and ADZUNA_APP_KEY.")

        keywords = " ".join([*criteria.keywords, *criteria.technologies]).strip()
        locations = criteria.locations or [""]
        opportunities: list[OpportunityIn] = []

        async with httpx.AsyncClient(timeout=25) as client:
            for location in locations:
                for page in range(1, criteria.max_pages + 1):
                    response = await client.get(
                        f"https://api.adzuna.com/v1/api/jobs/{self.settings.adzuna_country}/search/{page}",
                        params={
                            "app_id": self.settings.adzuna_app_id,
                            "app_key": self.settings.adzuna_app_key,
                            "what": keywords,
                            "where": location,
                            "results_per_page": criteria.results_per_page,
                            "content-type": "application/json",
                        },
                        headers={"Accept": "application/json"},
                    )
                    response.raise_for_status()
                    for item in response.json().get("results", []):
                        opportunity = self._normalize(item)
                        if _within_date_range(opportunity, criteria):
                            opportunities.append(opportunity)

        return opportunities

    def _normalize(self, item: dict) -> OpportunityIn:
        location = item.get("location") or {}
        company = item.get("company") or {}
        created = _parse_datetime(item.get("created"))
        return OpportunityIn(
            source="adzuna",
            external_id=str(item.get("id")) if item.get("id") is not None else None,
            title=item.get("title") or "Untitled",
            company=company.get("display_name") or "Unknown company",
            url=item.get("redirect_url"),
            location=location.get("display_name"),
            description=item.get("description"),
            posted_at=created,
            notes=_contract_notes(item),
        )


# ===========================================================================
# Restricted & Manual Catalog Providers (Classified & Documented)
# ===========================================================================

class RestrictedBaseProvider(DiscoveryProvider):
    async def search(self, criteria: SearchCriteria) -> list[OpportunityIn]:
        raise RuntimeError(self.restriction_reason or f"Automated access restricted for {self.name}.")


class IndeedMoroccoProvider(RestrictedBaseProvider):
    name = "indeed_morocco"
    access_method = "blocked/restricted"
    countries_covered = ["Morocco"]
    status = "blocked/restricted"
    restriction_reason = "Cloudflare anti-bot challenge (HTTP 403); manual ingestion available."


class IndeedFranceProvider(RestrictedBaseProvider):
    name = "indeed_france"
    access_method = "blocked/restricted"
    countries_covered = ["France"]
    status = "blocked/restricted"
    restriction_reason = "Cloudflare anti-bot challenge (HTTP 403); manual ingestion available."


class LinkedInProvider(RestrictedBaseProvider):
    name = "linkedin"
    access_method = "blocked/restricted"
    countries_covered = ["Morocco", "France", "International"]
    status = "blocked/restricted"
    restriction_reason = "Anti-bot protection and authentication required; manual ingestion available."


class RekruteProvider(RestrictedBaseProvider):
    name = "rekrute"
    access_method = "manual-only"
    countries_covered = ["Morocco"]
    status = "manual-only"
    restriction_reason = "Dynamic client-side AngularJS application without public API."


class EmploiMaProvider(RestrictedBaseProvider):
    name = "emploi_ma"
    access_method = "blocked/restricted"
    countries_covered = ["Morocco"]
    status = "blocked/restricted"
    restriction_reason = "Cloudflare anti-bot protection (HTTP 403)."


class NovojobProvider(RestrictedBaseProvider):
    name = "novojob"
    access_method = "manual-only"
    countries_covered = ["Morocco"]
    status = "manual-only"
    restriction_reason = "Session-gated job portal without public API."


class FranceTravailProvider(RestrictedBaseProvider):
    name = "france_travail"
    access_method = "API / structured public access"
    countries_covered = ["France"]
    status = "credentials_missing"
    restriction_reason = "Requires France Travail OAuth2 client_id and client_secret registration."


class ApecProvider(RestrictedBaseProvider):
    name = "apec"
    access_method = "blocked/restricted"
    countries_covered = ["France"]
    status = "blocked/restricted"
    restriction_reason = "JavaScript challenges and session anti-bot validation."


class WelcomeToTheJungleProvider(RestrictedBaseProvider):
    name = "welcome_to_the_jungle"
    access_method = "blocked/restricted"
    countries_covered = ["France"]
    status = "blocked/restricted"
    restriction_reason = "Anti-bot protected internal search index."


class JobTeaserProvider(RestrictedBaseProvider):
    name = "jobteaser"
    access_method = "blocked/restricted"
    countries_covered = ["France", "Europe"]
    status = "blocked/restricted"
    restriction_reason = "Cloudflare anti-bot protection (HTTP 403)."


class StagiairesMaProvider(DiscoveryProvider):
    name = "stagiaires_ma"
    access_method = "public page access"
    countries_covered = ["Morocco"]
    status = "active"

    async def search(self, criteria: SearchCriteria) -> list[OpportunityIn]:
        url = "https://stagiaires.ma/stage-emploi-maroc"
        opportunities: list[OpportunityIn] = []
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            scripts = re.findall(
                r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                response.text,
                re.DOTALL,
            )
            for script in scripts:
                try:
                    data = json.loads(script)
                    if isinstance(data, dict) and data.get("@type") == "JobPosting":
                        opp = self._normalize(data)
                        if opp and _matches_criteria(opp, criteria):
                            opportunities.append(opp)
                    elif isinstance(data, list):
                        for item in data:
                            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                                opp = self._normalize(item)
                                if opp and _matches_criteria(opp, criteria):
                                    opportunities.append(opp)
                except Exception:
                    continue
        return opportunities

    def _normalize(self, data: dict) -> OpportunityIn | None:
        title = data.get("title")
        url = data.get("url")
        if not title or not url:
            return None
        org = data.get("hiringOrganization") or {}
        company = org.get("name") if isinstance(org, dict) else "Unknown company"
        loc_obj = data.get("jobLocation") or {}
        address = loc_obj.get("address") if isinstance(loc_obj, dict) else {}
        city = address.get("addressLocality") if isinstance(address, dict) else None
        location = f"{city}, Morocco" if city else "Morocco"
        ext_id = str(url).rstrip("/").split("/")[-1]

        return OpportunityIn(
            source="stagiaires_ma",
            external_id=ext_id,
            title=str(title).strip(),
            company=str(company).strip() or "Unknown company",
            url=str(url).strip(),
            location=location,
            description=clean_html(data.get("description")),
            posted_at=_parse_datetime(data.get("datePosted")),
        )


class WeWorkRemotelyProvider(DiscoveryProvider):
    name = "weworkremotely"
    access_method = "public page access"
    countries_covered = ["Remote", "International"]
    status = "active"

    async def search(self, criteria: SearchCriteria) -> list[OpportunityIn]:
        url = "https://weworkremotely.com/categories/remote-programming-jobs.rss"
        opportunities: list[OpportunityIn] = []
        headers = {"User-Agent": "Mozilla/5.0"}
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            try:
                import xml.etree.ElementTree as ET
                root = ET.fromstring(response.text)
                channel = root.find("channel")
                if channel is not None:
                    for item in channel.findall("item"):
                        raw_title = item.findtext("title") or ""
                        link = item.findtext("link") or ""
                        pub_date = item.findtext("pubDate")
                        desc = item.findtext("description")
                        parts = raw_title.split(":", 1)
                        if len(parts) == 2:
                            company = parts[0].strip()
                            title = parts[1].strip()
                        else:
                            company = "Unknown company"
                            title = raw_title.strip()
                        ext_id = link.rstrip("/").split("/")[-1] if link else None
                        opp = OpportunityIn(
                            source="weworkremotely",
                            external_id=ext_id,
                            title=title or "Untitled",
                            company=company,
                            url=link,
                            location="Remote",
                            description=clean_html(desc),
                            posted_at=_parse_datetime(pub_date),
                        )
                        if _matches_criteria(opp, criteria):
                            opportunities.append(opp)
            except Exception:
                pass
        return opportunities


class RemoteOKProvider(DiscoveryProvider):
    name = "remoteok"
    access_method = "API / structured public access"
    countries_covered = ["Remote", "International"]
    status = "active"

    async def search(self, criteria: SearchCriteria) -> list[OpportunityIn]:
        url = "https://remoteok.com/api"
        opportunities: list[OpportunityIn] = []
        headers = {"User-Agent": "Mozilla/5.0"}
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            items = response.json()
            for item in items:
                if isinstance(item, dict) and item.get("slug"):
                    loc = item.get("location") or "Remote"
                    dt = _parse_datetime(item.get("date"))
                    opp = OpportunityIn(
                        source="remoteok",
                        external_id=str(item.get("slug")),
                        title=item.get("position") or "Untitled",
                        company=item.get("company") or "Unknown company",
                        url=item.get("url") or f"https://remoteok.com/remote-jobs/{item.get('slug')}",
                        location=loc if "remote" in loc.lower() else f"{loc} (Remote)",
                        description=clean_html(item.get("description")),
                        posted_at=dt,
                    )
                    if _matches_criteria(opp, criteria):
                        opportunities.append(opp)
        return opportunities


class PfeDabaProvider(DiscoveryProvider):
    """PFE Daba — Moroccan PFE aggregator with public JSON API.

    Aggregates from: Stagiaires.ma, Indeed, LinkedIn, Rekrute, Optioncarriere.
    API: GET /api/internships?page=N  (returns JSON with pagination).
    Each listing includes ``provider`` and ``provider_link`` for source provenance.
    """

    name = "pfedaba"
    access_method = "API / structured public access"
    countries_covered = ["Morocco"]
    status = "active"

    _BASE = "https://pfedaba.ma/api/internships"

    async def search(self, criteria: SearchCriteria) -> list[OpportunityIn]:
        opportunities: list[OpportunityIn] = []
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
        }
        max_pages = min(criteria.max_pages, 10)
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            page = 1
            total_pages = 1
            while page <= min(total_pages, max_pages):
                response = await client.get(
                    self._BASE,
                    params={"page": page},
                    headers=headers,
                )
                response.raise_for_status()
                data = response.json()
                total_pages = data.get("total_pages", 1)
                for item in data.get("data", []):
                    opp = self._normalize(item)
                    if opp and _matches_criteria(opp, criteria):
                        opportunities.append(opp)
                page += 1
        return opportunities

    def _normalize(self, item: dict) -> OpportunityIn | None:
        title = (item.get("title") or "").strip()
        company = (item.get("company_name") or "Unknown company").strip()
        if not title:
            return None

        # Determine the best URL: prefer the external link (Indeed/LinkedIn/etc.)
        link = item.get("link")  # external apply URL
        slug = item.get("slug") or ""
        pfedaba_url = f"https://pfedaba.ma/stages/{slug}" if slug else None
        url = link or pfedaba_url
        if not url:
            return None

        # Build source provenance
        original_provider = (item.get("provider") or "").strip()
        provider_link = (item.get("provider_link") or "").strip()
        is_direct = item.get("is_direct", False)

        if is_direct:
            source_label = f"pfedaba:direct"
        elif original_provider:
            source_label = f"pfedaba:{original_provider.lower().replace('.', '_').replace(' ', '_')}"
        else:
            source_label = "pfedaba"

        # Truncate source to schema max_length (80 chars)
        source_label = source_label[:80]

        location = (item.get("location") or item.get("city") or "Morocco").strip()
        if location and "morocco" not in location.lower() and "maroc" not in location.lower() and ", MA" not in location:
            location = f"{location}, Morocco"

        # Build notes with provenance
        notes_parts = []
        if original_provider:
            notes_parts.append(f"original_source={original_provider}")
        if provider_link:
            notes_parts.append(f"original_source_url={provider_link}")
        if link and pfedaba_url:
            notes_parts.append(f"pfedaba_url={pfedaba_url}")
        domain = (item.get("field_domain") or "").strip()
        if domain:
            notes_parts.append(f"domain={domain}")
        work_mode = (item.get("work_mode") or "").strip()
        if work_mode:
            notes_parts.append(f"work_mode={work_mode}")
        duration = item.get("duration_months")
        if duration:
            notes_parts.append(f"duration={duration}m")
        salary = (item.get("salary_amount") or "").strip()
        if salary:
            notes_parts.append(f"salary={salary}")

        desc = clean_html(item.get("description"))

        return OpportunityIn(
            source=source_label,
            external_id=str(item.get("id") or slug),
            title=title,
            company=company,
            url=url,
            location=location,
            description=desc,
            posted_at=_parse_datetime(item.get("posted_date")),
            notes="; ".join(notes_parts) if notes_parts else None,
        )


class OptioncarriereProvider(RestrictedBaseProvider):
    name = "optioncarriere"
    access_method = "blocked/restricted"
    countries_covered = ["Morocco", "France"]
    status = "blocked/restricted"
    restriction_reason = "Cloudflare bot protection (HTTP 403). Discovered through PFE Daba aggregator; accessible indirectly via pfedaba."


class JoobleProvider(RestrictedBaseProvider):
    name = "jooble"
    access_method = "blocked/restricted"
    countries_covered = ["Morocco", "France", "International"]
    status = "blocked/restricted"
    restriction_reason = "Cloudflare bot protection (HTTP 403). Discovered through PFE Daba aggregator; accessible indirectly via pfedaba."


class MarocAnnoncesProvider(RestrictedBaseProvider):
    name = "marocannonces"
    access_method = "manual-only"
    countries_covered = ["Morocco"]
    status = "manual-only"
    restriction_reason = "Classified ads portal without structured IT job API."


# ===========================================================================
# Provider Builder and Registry
# ===========================================================================

def build_providers(criteria: SearchCriteria, settings: Settings) -> list[DiscoveryProvider]:
    providers: list[DiscoveryProvider] = []
    requested = set(criteria.providers)

    # Active providers
    if "stagiaires_ma" in requested or "all" in requested:
        providers.append(StagiairesMaProvider())
    if "greenhouse" in requested or "all" in requested:
        providers.append(GreenhouseProvider(criteria.greenhouse_boards))
    if "lever" in requested or "all" in requested:
        providers.append(LeverProvider(criteria.lever_sites))
    if "remotive" in requested or "all" in requested:
        providers.append(RemotiveProvider())
    if "arbeitnow" in requested or "all" in requested:
        providers.append(ArbeitnowProvider())
    if "jobicy" in requested or "all" in requested:
        providers.append(JobicyProvider())
    if "weworkremotely" in requested or "all" in requested:
        providers.append(WeWorkRemotelyProvider())
    if "remoteok" in requested or "all" in requested:
        providers.append(RemoteOKProvider())
    if "adzuna" in requested or "all" in requested:
        providers.append(AdzunaProvider(settings))
    if "pfedaba" in requested or "all" in requested:
        providers.append(PfeDabaProvider())
    if "web_search" in requested or "all" in requested:
        from app.integrations.web_search import WebSearchDiscoveryProvider
        providers.append(WebSearchDiscoveryProvider(settings))

    # Restricted / manual providers (if explicitly requested)
    restricted_map = {
        "indeed_morocco": IndeedMoroccoProvider,
        "indeed_france": IndeedFranceProvider,
        "linkedin": LinkedInProvider,
        "rekrute": RekruteProvider,
        "emploi_ma": EmploiMaProvider,
        "novojob": NovojobProvider,
        "marocannonces": MarocAnnoncesProvider,
        "france_travail": FranceTravailProvider,
        "apec": ApecProvider,
        "welcome_to_the_jungle": WelcomeToTheJungleProvider,
        "jobteaser": JobTeaserProvider,
        "optioncarriere": OptioncarriereProvider,
        "jooble": JoobleProvider,
    }
    for name, cls in restricted_map.items():
        if name in requested:
            providers.append(cls())

    return providers


def get_all_investigated_providers(settings: Settings) -> list[DiscoveryProvider]:
    """Returns the complete list of investigated recruitment providers across all classifications."""
    from app.integrations.web_search import WebSearchDiscoveryProvider
    return [
        StagiairesMaProvider(),
        GreenhouseProvider(["doctolib", "canonical", "datadog", "algolia", "gitlab"]),
        LeverProvider(["blablacar", "scaleway", "malt", "brevo", "contentsquare"]),
        RemotiveProvider(),
        ArbeitnowProvider(),
        JobicyProvider(),
        WeWorkRemotelyProvider(),
        RemoteOKProvider(),
        AdzunaProvider(settings),
        IndeedMoroccoProvider(),
        IndeedFranceProvider(),
        LinkedInProvider(),
        RekruteProvider(),
        EmploiMaProvider(),
        NovojobProvider(),
        MarocAnnoncesProvider(),
        FranceTravailProvider(),
        ApecProvider(),
        WelcomeToTheJungleProvider(),
        JobTeaserProvider(),
        PfeDabaProvider(),
        OptioncarriereProvider(),
        JoobleProvider(),
        WebSearchDiscoveryProvider(settings),
    ]


# ===========================================================================
# Helpers
# ===========================================================================

def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        clean_val = value.replace("Z", "+00:00")
        return datetime.fromisoformat(clean_val).astimezone(timezone.utc)
    except Exception:
        try:
            return datetime.strptime(value[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except Exception:
            return None


def _within_date_range(opportunity: OpportunityIn, criteria: SearchCriteria) -> bool:
    if not opportunity.posted_at:
        return True
    posted = opportunity.posted_at.date()
    if criteria.date_from and posted < criteria.date_from:
        return False
    if criteria.date_to and posted > criteria.date_to:
        return False
    return True


def _matches_criteria(opportunity: OpportunityIn, criteria: SearchCriteria) -> bool:
    if not _within_date_range(opportunity, criteria):
        return False
    text = " ".join(
        value or ""
        for value in (
            opportunity.title,
            opportunity.company,
            opportunity.location,
            opportunity.description,
        )
    ).lower()
    query_terms = [*criteria.keywords, *criteria.technologies]
    if query_terms and not any(term.lower() in text for term in query_terms):
        return False
    if criteria.locations and not any(
        location.lower() in text for location in criteria.locations
    ):
        return False
    return True


def _contract_notes(item: dict) -> str | None:
    parts = []
    for key in ("contract_type", "contract_time"):
        if item.get(key):
            parts.append(f"{key}: {item[key]}")
    return "; ".join(parts) or None
