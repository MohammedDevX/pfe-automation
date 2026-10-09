"""Advanced Web Search Discovery Strategy Engine.

Provides web search query generation, pluggable search engine adapters (DuckDuckGo,
Custom API, Mock), URL normalization, search result filtering, page type detection,
Schema.org JobPosting / OpenGraph / HTML extraction, and WebSearchDiscoveryProvider.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import asyncio
from datetime import datetime, timezone
import json
import re
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlparse, urlunparse

import httpx

from app.database import Settings
from app.integrations.ats import clean_html
from app.integrations.providers import DiscoveryProvider
from app.schemas import OpportunityIn, SearchCriteria


# ===========================================================================
# Data Structures
# ===========================================================================

@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str
    engine: str
    rank: int = 0


@dataclass
class WebSearchMetrics:
    queries_executed: int = 0
    results_seen: int = 0
    candidate_urls_found: int = 0
    pages_fetched: int = 0
    opportunities_extracted: int = 0
    new_opportunities: int = 0
    duplicates_ignored: int = 0
    rejected_low_score: int = 0
    errors: list[str] = field(default_factory=list)
    discovered_domains: set[str] = field(default_factory=set)


# ===========================================================================
# Search Engine Adapter Abstraction & Implementations
# ===========================================================================

class SearchEngineAdapter(ABC):
    name: str

    @abstractmethod
    async def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        raise NotImplementedError


class DuckDuckGoSearchEngine(SearchEngineAdapter):
    """Real public search engine adapter using DuckDuckGo HTML/Lite endpoints."""

    name = "duckduckgo"

    def __init__(self, timeout: float = 10.0):
        self.timeout = timeout
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": "https://duckduckgo.com/",
        }

    async def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        results: list[SearchResult] = []
        url = "https://html.duckduckgo.com/html/"
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            try:
                resp = await client.post(url, data={"q": query}, headers=self.headers)
                if resp.status_code == 200:
                    results = self._parse_html_results(resp.text, limit)
            except Exception:
                # Fallback to Lite endpoint if HTML fails
                try:
                    lite_url = "https://lite.duckduckgo.com/lite/"
                    resp_lite = await client.post(lite_url, data={"q": query}, headers=self.headers)
                    if resp_lite.status_code == 200:
                        results = self._parse_lite_results(resp_lite.text, limit)
                except Exception:
                    pass
        return results[:limit]

    def _parse_html_results(self, html: str, limit: int) -> list[SearchResult]:
        results: list[SearchResult] = []
        blocks = re.findall(r'<div[^>]*class="[^"]*result[^"]*"[^>]*>(.*?)</div>\s*</div>', html, re.DOTALL)
        rank = 1
        for block in blocks:
            title_m = re.search(r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*>(.*?)</a>', block, re.DOTALL)
            snippet_m = re.search(r'<(?:a|td|div)[^>]*class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</(?:a|td|div)>', block, re.DOTALL)
            if not title_m:
                continue

            raw_title = title_m.group(1)
            url_m = re.search(r'href="([^"]+)"', title_m.group(0))
            if not url_m:
                continue

            raw_url = url_m.group(1)
            target_url = self._extract_target_url(raw_url)
            if not target_url:
                continue

            clean_title = re.sub(r'<[^>]+>', '', raw_title).strip()
            clean_snippet = re.sub(r'<[^>]+>', '', snippet_m.group(1)).strip() if snippet_m else ""

            results.append(SearchResult(
                title=clean_title,
                url=target_url,
                snippet=clean_snippet,
                engine=self.name,
                rank=rank
            ))
            rank += 1
            if len(results) >= limit:
                break
        return results

    def _parse_lite_results(self, html: str, limit: int) -> list[SearchResult]:
        results: list[SearchResult] = []
        links = re.findall(r'<a[^>]*href="([^"]+)"[^>]*class="result-link"[^>]*>(.*?)</a>', html, re.DOTALL)
        snippets = re.findall(r'<td[^>]*class="result-snippet"[^>]*>(.*?)</td>', html, re.DOTALL)
        rank = 1
        for idx, (raw_url, raw_title) in enumerate(links):
            target_url = self._extract_target_url(raw_url)
            if not target_url:
                continue
            clean_title = re.sub(r'<[^>]+>', '', raw_title).strip()
            clean_snippet = re.sub(r'<[^>]+>', '', snippets[idx]).strip() if idx < len(snippets) else ""
            results.append(SearchResult(
                title=clean_title,
                url=target_url,
                snippet=clean_snippet,
                engine=self.name,
                rank=rank
            ))
            rank += 1
            if len(results) >= limit:
                break
        return results

    def _extract_target_url(self, raw_url: str) -> str | None:
        if not raw_url:
            return None
        if "uddg=" in raw_url:
            try:
                parsed = parse_qs(urlparse(raw_url).query)
                if "uddg" in parsed:
                    return unquote(parsed["uddg"][0])
            except Exception:
                pass
            try:
                return unquote(raw_url.split("uddg=")[1].split("&")[0])
            except Exception:
                pass
        if raw_url.startswith("http://") or raw_url.startswith("https://"):
            return raw_url
        return None


class CustomApiSearchEngine(SearchEngineAdapter):
    """Search engine adapter using a configured Search API key (SerpAPI / Custom Search API)."""

    name = "custom_api"

    def __init__(self, api_key: str, endpoint: str | None = None):
        self.api_key = api_key
        self.endpoint = endpoint or "https://serpapi.com/search.json"

    async def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        results: list[SearchResult] = []
        params = {"q": query, "api_key": self.api_key, "num": limit}
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(self.endpoint, params=params)
            resp.raise_for_status()
            data = resp.json()
            organic = data.get("organic_results", [])
            for idx, item in enumerate(organic[:limit], start=1):
                results.append(SearchResult(
                    title=item.get("title") or "",
                    url=item.get("link") or "",
                    snippet=item.get("snippet") or "",
                    engine=self.name,
                    rank=idx,
                ))
        return results


class BraveSearchEngine(SearchEngineAdapter):
    """Search engine adapter using Brave Search API."""

    name = "brave"

    def __init__(self, api_key: str | None = None, endpoint: str | None = None, timeout: float = 10.0):
        self.api_key = api_key
        self.endpoint = endpoint or "https://api.search.brave.com/res/v1/web/search"
        self.timeout = timeout

    async def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        if not self.api_key:
            return []
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-Subscription-Token": self.api_key,
        }
        params = {"q": query, "count": min(limit, 20)}
        results: list[SearchResult] = []
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(self.endpoint, params=params, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            web_results = data.get("web", {}).get("results", [])
            for idx, item in enumerate(web_results[:limit], start=1):
                results.append(
                    SearchResult(
                        title=item.get("title") or "",
                        url=item.get("url") or "",
                        snippet=item.get("description") or "",
                        engine=self.name,
                        rank=idx,
                    )
                )
        return results


class MultiSearchEngineAdapter(SearchEngineAdapter):
    """Composite adapter executing searches across multiple search engine adapters safely."""

    name = "multi"

    def __init__(self, adapters: list[SearchEngineAdapter]):
        self.adapters = [a for a in adapters if a is not None]

    async def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        if not self.adapters:
            return []
        if len(self.adapters) == 1:
            try:
                return await self.adapters[0].search(query, limit=limit)
            except Exception:
                return []

        merged_results: list[SearchResult] = []
        seen_urls: set[str] = set()

        for adapter in self.adapters:
            try:
                engine_results = await adapter.search(query, limit=limit)
                for res in engine_results:
                    cleaned_u = SearchResultFilter.clean_url(res.url)
                    target_key = cleaned_u or res.url
                    if target_key not in seen_urls:
                        seen_urls.add(target_key)
                        merged_results.append(res)
            except Exception:
                # Isolate failure per engine: continue with remaining engines
                pass

        return merged_results[:limit]


class MockSearchEngine(SearchEngineAdapter):
    """Deterministic mock search engine adapter for unit testing."""

    name = "mock"

    def __init__(self, responses: dict[str, list[SearchResult]] | None = None):
        self.responses = responses or {}

    async def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        q_lower = query.lower()
        for k, v in self.responses.items():
            k_lower = k.lower()
            if k_lower in q_lower or q_lower in k_lower:
                return v[:limit]
        return []


def build_search_engine(settings: Settings) -> SearchEngineAdapter | None:
    """Builds and returns the configured search engine adapter, or None if disabled/unconfigured."""
    engine_type = (settings.web_search_engine or "duckduckgo").lower()
    if engine_type in ("none", "disabled"):
        return None

    adapters: list[SearchEngineAdapter] = []

    if engine_type == "custom_api":
        if settings.web_search_api_key:
            adapters.append(CustomApiSearchEngine(settings.web_search_api_key))
        else:
            return None
    elif engine_type == "brave":
        if getattr(settings, "brave_search_api_key", None):
            adapters.append(BraveSearchEngine(settings.brave_search_api_key))
        else:
            return None
    elif engine_type in ("multi", "duckduckgo"):
        if settings.web_search_api_key:
            adapters.append(CustomApiSearchEngine(settings.web_search_api_key))
        if getattr(settings, "brave_search_api_key", None):
            adapters.append(BraveSearchEngine(settings.brave_search_api_key))
        adapters.append(DuckDuckGoSearchEngine())
    else:
        adapters.append(DuckDuckGoSearchEngine())

    if not adapters:
        return None
    if len(adapters) == 1:
        return adapters[0]
    return MultiSearchEngineAdapter(adapters)


# ===========================================================================
# Query Generator & Prioritization Engine
# ===========================================================================

class WebSearchQueryGenerator:
    """Generates prioritized, deterministic search queries with advanced operators."""

    PFE_KEYWORDS_FR = [
        "stage PFE",
        "stage de fin d'études",
        "projet de fin d'études",
        "stage fin d'études",
        "stage pré-embauche",
    ]

    INTERNSHIP_KEYWORDS_EN = [
        "final year internship",
        "graduation internship",
        "software engineering internship",
        "backend internship",
    ]

    CANDIDATE_TECHS = [
        ".NET",
        "C#",
        "ASP.NET Core",
        "Angular",
        "Java",
        "Spring Boot",
        "PHP",
        "Symfony",
        "React",
        "JavaScript",
        "Backend",
        "Full Stack",
        "Software Engineering",
        "DevOps",
        "CI/CD",
        "Docker",
        "QA",
        "Software Testing",
        "AI",
        "Machine Learning",
        "Automation",
        "Data Engineering",
    ]

    CITIES_MOROCCO = [
        "Casablanca",
        "Rabat",
        "Tanger",
        "Marrakech",
        "Agadir",
        "Maroc",
    ]

    LOCATIONS_FRANCE_REMOTE = [
        "France",
        "Paris",
        "Remote",
    ]

    def generate_queries(self, criteria: SearchCriteria, max_queries: int = 30) -> list[str]:
        queries: list[str] = []
        seen: set[str] = set()

        def add_q(q: str):
            clean_q = re.sub(r'\s+', ' ', q).strip()
            if clean_q and clean_q not in seen:
                seen.add(clean_q)
                queries.append(clean_q)

        # -------------------------------------------------------------------
        # TIER 1: High-priority explicit PFE / Internship & Platform Queries
        # -------------------------------------------------------------------
        for tech in [".NET", "C#", "ASP.NET Core", "Angular", "Java", "Spring Boot", "React", "Python", "Full Stack", "Backend", "DevOps"]:
            add_q(f'"stage PFE" "{tech}" Maroc')
            add_q(f'"stage PFE" "{tech}" Casablanca')

        # Public Job Boards & ATS Platforms
        add_q('site:linkedin.com/jobs/view "stage PFE"')
        add_q('site:linkedin.com/jobs/view "stage de fin d\'études" ".NET"')
        add_q('site:linkedin.com/jobs/view "software engineer intern" France')
        add_q('site:linkedin.com/jobs/view "backend" "internship"')

        add_q('site:welcometothejungle.com "stage" ".NET"')
        add_q('site:welcometothejungle.com "stage" "backend"')
        add_q('site:welcometothejungle.com "internship" "software engineer"')

        add_q('site:hellowork.com "stage PFE" ".NET"')
        add_q('site:hellowork.com "stage" "développeur .NET"')
        add_q('site:hellowork.com "stage" "développeur backend"')

        add_q('site:jobs.smartrecruiters.com "stage" ".NET"')
        add_q('site:jobs.smartrecruiters.com "internship" "software engineer"')
        add_q('site:jobs.smartrecruiters.com "internship" "backend"')

        add_q('site:jobs.ashbyhq.com "internship" "software engineer"')
        add_q('site:jobs.ashbyhq.com "internship" "backend"')

        add_q('site:apply.workable.com "internship" "software engineer"')
        add_q('site:apply.workable.com "stage" ".NET"')

        # -------------------------------------------------------------------
        # TIER 2: Remaining Candidate Technologies + Locations & ATS URL patterns
        # -------------------------------------------------------------------
        for tech in self.CANDIDATE_TECHS:
            add_q(f'"stage PFE" "{tech}" Rabat')
            add_q(f'"stage fin d\'études" "{tech}" Maroc')

        add_q('"stage pré-embauche" développeur Casablanca')
        add_q('"stage PFE" informatique Maroc')
        add_q('inurl:jobs "stage PFE" ".NET"')
        add_q('inurl:careers "stage PFE" "Spring Boot"')

        # -------------------------------------------------------------------
        # TIER 3: Tech + France / Remote / International Internship
        # -------------------------------------------------------------------
        for tech in [".NET", "C#", "Angular", "Java", "React", "PHP", "backend", "DevOps"]:
            add_q(f'"final year internship" "{tech}" France')
            add_q(f'"stage fin d\'études" "{tech}" Paris')
            add_q(f'"graduation internship" "{tech}" remote')

        add_q('inurl:careers "final year internship" ".NET"')
        add_q('site:jobs.lever.co "internship" ".NET"')
        add_q('site:boards.greenhouse.io "internship" "backend"')

        # -------------------------------------------------------------------
        # TIER 4: Generic & Custom Criteria Keywords
        # -------------------------------------------------------------------
        if criteria.keywords or criteria.technologies:
            user_terms = [*criteria.keywords, *criteria.technologies]
            for term in user_terms[:6]:
                add_q(f'"stage PFE" "{term}"')
                add_q(f'"final year internship" "{term}"')

        return queries[:max_queries]


# ===========================================================================
# URL Normalizer, Search Result Filter & Page Type Detector
# ===========================================================================

class SearchResultFilter:
    """Filters search result URLs and cleans tracking parameters."""

    IRRELEVANT_PATTERNS = [
        r'wikipedia\.org',
        r'youtube\.com',
        r'facebook\.com',
        r'twitter\.com',
        r'instagram\.com',
        r'amazon\.com',
        r'github\.com/[^/]+/[^/]+$',  # repo homepages, not job posts
        r'medium\.com',
        r'udemy\.com',
        r'coursera\.org',
        r'glassdoor\.com/Salaries',
        r'indeed\.com/career-advice',
        r'blog\.',
        r'/blog/',
        r'/news/',
        r'/courses/',
        r'/article/',
    ]

    JOB_PATH_HINTS = [
        "/jobs/", "/job/", "/careers/", "/career/", "/vacancies/", "/vacancy/",
        "/internships/", "/internship/", "/stages/", "/stage/", "/offres/",
        "/emploi/", "/positions/", "/postings/", "/apply/"
    ]

    @classmethod
    def clean_url(cls, url: str) -> str:
        """Removes tracking query parameters, fragments, and standardizes URL."""
        if not url:
            return ""
        try:
            parsed = urlparse(url)
            # Filter out standard tracking params
            query_params = parse_qs(parsed.query)
            clean_params = {
                k: v for k, v in query_params.items()
                if not k.lower().startswith("utm") and k.lower() not in {"ref", "fbclid", "gclid", "_hsenc", "_hsmi", "trk", "source"}
            }
            # Rebuild query string
            new_query = "&".join(f"{k}={v[0]}" for k, v in clean_params.items())
            cleaned = urlunparse((
                parsed.scheme,
                parsed.netloc.lower(),
                parsed.path.rstrip("/") if len(parsed.path) > 1 else parsed.path,
                parsed.params,
                new_query,
                ""  # drop fragment
            ))
            return cleaned
        except Exception:
            return url

    @classmethod
    def is_relevant_search_result(cls, result: SearchResult) -> bool:
        """Evaluates whether a search result is worth fetching."""
        url = result.url.lower()
        title = result.title.lower()
        snippet = result.snippet.lower()

        # Reject obvious non-job patterns
        for pattern in cls.IRRELEVANT_PATTERNS:
            if re.search(pattern, url, re.IGNORECASE):
                return False

        # Positive URL path hint
        if any(hint in url for hint in cls.JOB_PATH_HINTS):
            return True

        # Positive title/snippet signals
        job_keywords = ["stage", "pfe", "intern", "internship", "recrute", "job", "career", "développeur", "engineer"]
        if any(kw in title for kw in job_keywords) or any(kw in snippet for kw in job_keywords):
            return True

        # Do NOT reject generic URLs unless there are strong negative signals
        return True


class PageTypeDetector:
    """Detects ATS systems and page category."""

    @classmethod
    def detect_ats(cls, url: str) -> dict[str, str] | None:
        """Detects if URL belongs to a known ATS (Greenhouse, Lever, SmartRecruiters, Workable, etc.)."""
        url_lower = url.lower()
        if "boards.greenhouse.io" in url_lower:
            m = re.search(r'boards\.greenhouse\.io/([^/]+)/jobs/(\d+)', url_lower)
            if m:
                return {"ats": "greenhouse", "board": m.group(1), "external_id": m.group(2)}
            return {"ats": "greenhouse"}

        if "jobs.lever.co" in url_lower:
            m = re.search(r'jobs\.lever\.co/([^/]+)/([a-f0-9\-]+)', url_lower)
            if m:
                return {"ats": "lever", "site": m.group(1), "external_id": m.group(2)}
            return {"ats": "lever"}

        if "smartrecruiters.com" in url_lower:
            return {"ats": "smartrecruiters"}
        if "workable.com" in url_lower:
            return {"ats": "workable"}
        if "ashbyhq.com" in url_lower:
            return {"ats": "ashby"}
        if "teamtailor.com" in url_lower:
            return {"ats": "teamtailor"}
        if "recruitee.com" in url_lower:
            return {"ats": "recruitee"}
        if "welcometothejungle.com" in url_lower:
            return {"ats": "welcome_to_the_jungle"}

        return None


# ===========================================================================
# Job Posting Content Extractor (JSON-LD -> OpenGraph -> HTML)
# ===========================================================================

class JobPostingExtractor:
    """Extracts structured job posting details using JSON-LD, OpenGraph, and HTML fallbacks."""

    @classmethod
    def extract_from_html(cls, html: str, page_url: str) -> dict[str, Any] | None:
        if not html:
            return None

        # Priority 1: Schema.org JobPosting JSON-LD
        json_ld_job = cls._extract_json_ld(html)
        if json_ld_job:
            extracted = cls._parse_job_posting_json_ld(json_ld_job, page_url)
            if extracted and extracted.get("title"):
                return extracted

        # Priority 2: OpenGraph & Meta Tags
        meta_job = cls._extract_opengraph(html, page_url)
        if meta_job and meta_job.get("title"):
            return meta_job

        # Priority 3: Fallback content heuristics
        heuristic_job = cls._extract_heuristics(html, page_url)
        if heuristic_job and heuristic_job.get("title"):
            return heuristic_job

        return None

    @classmethod
    def _extract_json_ld(cls, html: str) -> dict | list | None:
        """Extracts JobPosting schema from single objects, lists, or @graph arrays."""
        scripts = re.findall(
            r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html,
            re.DOTALL | re.IGNORECASE,
        )
        for script in scripts:
            try:
                data = json.loads(script.strip())
                job_obj = cls._find_job_posting_in_data(data)
                if job_obj:
                    return job_obj
            except Exception:
                continue
        return None

    @classmethod
    def _find_job_posting_in_data(cls, data: Any) -> dict | None:
        """Recursively finds a @type == 'JobPosting' dict in data."""
        if isinstance(data, dict):
            t = data.get("@type")
            if t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t):
                return data
            if "@graph" in data and isinstance(data["@graph"], list):
                for item in data["@graph"]:
                    res = cls._find_job_posting_in_data(item)
                    if res:
                        return res
            for k, v in data.items():
                if isinstance(v, (dict, list)):
                    res = cls._find_job_posting_in_data(v)
                    if res:
                        return res
        elif isinstance(data, list):
            for item in data:
                res = cls._find_job_posting_in_data(item)
                if res:
                    return res
        return None

    @classmethod
    def _parse_job_posting_json_ld(cls, data: dict, page_url: str) -> dict[str, Any]:
        title = data.get("title") or data.get("name")
        org = data.get("hiringOrganization") or data.get("publisher") or {}
        company = org.get("name") if isinstance(org, dict) else str(org) if org else None

        loc_obj = data.get("jobLocation") or {}
        location = None
        if isinstance(loc_obj, dict):
            address = loc_obj.get("address") or {}
            if isinstance(address, dict):
                city = address.get("addressLocality")
                country = address.get("addressCountry")
                if city and country:
                    location = f"{city}, {country}"
                elif city:
                    location = city
                elif country:
                    location = str(country)
            elif isinstance(address, str):
                location = address
        elif isinstance(loc_obj, list) and loc_obj:
            first_loc = loc_obj[0]
            if isinstance(first_loc, dict):
                addr = first_loc.get("address") or {}
                if isinstance(addr, dict):
                    location = addr.get("addressLocality")

        desc = data.get("description")
        date_posted = data.get("datePosted")
        valid_through = data.get("validThrough")

        canonical_url = data.get("url") or page_url

        return {
            "title": str(title).strip() if title else None,
            "company": str(company).strip() if company else "Unknown company",
            "location": str(location).strip() if location else None,
            "description": clean_html(desc) if desc else None,
            "posted_at": date_posted,
            "deadline": valid_through,
            "canonical_url": canonical_url,
            "extraction_method": "json_ld",
        }

    @classmethod
    def _extract_opengraph(cls, html: str, page_url: str) -> dict[str, Any] | None:
        title_m = (
            re.search(r'<meta[^>]*property=["\']og:title["\'][^>]*content=["\']([^"\']+)["\']', html, re.I)
            or re.search(r'<title[^>]*>(.*?)</title>', html, re.I | re.DOTALL)
        )
        if not title_m:
            return None

        title = clean_html(title_m.group(1)).strip()

        desc_m = (
            re.search(r'<meta[^>]*property=["\']og:description["\'][^>]*content=["\']([^"\']+)["\']', html, re.I)
            or re.search(r'<meta[^>]*name=["\']description["\'][^>]*content=["\']([^"\']+)["\']', html, re.I)
        )
        desc = clean_html(desc_m.group(1)).strip() if desc_m else None

        site_m = re.search(r'<meta[^>]*property=["\']og:site_name["\'][^>]*content=["\']([^"\']+)["\']', html, re.I)
        company = site_m.group(1).strip() if site_m else urlparse(page_url).netloc.replace("www.", "")

        canonical_m = re.search(r'<link[^>]*rel=["\']canonical["\'][^>]*href=["\']([^"\']+)["\']', html, re.I)
        canonical_url = canonical_m.group(1) if canonical_m else page_url

        return {
            "title": title,
            "company": company,
            "location": None,
            "description": desc,
            "posted_at": None,
            "deadline": None,
            "canonical_url": canonical_url,
            "extraction_method": "opengraph",
        }

    @classmethod
    def _extract_heuristics(cls, html: str, page_url: str) -> dict[str, Any] | None:
        title_m = re.search(r'<h1[^>]*>(.*?)</h1>', html, re.I | re.DOTALL)
        if not title_m:
            return None
        title = clean_html(title_m.group(1)).strip()
        if len(title) < 5 or len(title) > 200:
            return None
        company = urlparse(page_url).netloc.replace("www.", "")
        return {
            "title": title,
            "company": company,
            "location": None,
            "description": clean_html(html[:2000]),
            "posted_at": None,
            "deadline": None,
            "canonical_url": page_url,
            "extraction_method": "heuristics",
        }


# ===========================================================================
# WebSearchDiscoveryProvider (Discovery Engine Integration)
# ===========================================================================

class WebSearchDiscoveryProvider(DiscoveryProvider):
    """Additive Discovery Provider that searches public web results and extracts job postings."""

    name = "web_search"
    access_method = "public page access"
    countries_covered = ["Morocco", "France", "Remote", "International"]

    def __init__(self, settings: Settings, search_engine: SearchEngineAdapter | None = None):
        self.settings = settings
        self.engine = search_engine or build_search_engine(settings)
        if not self.engine:
            self.status = "credentials_missing"
            self.restriction_reason = "No active search engine adapter configured for Web Search."
        else:
            self.status = "active"

        self.query_generator = WebSearchQueryGenerator()
        self.metrics = WebSearchMetrics()

    async def search(self, criteria: SearchCriteria) -> list[OpportunityIn]:
        if not self.engine:
            raise RuntimeError(self.restriction_reason or "Web Search engine unconfigured.")

        max_queries = getattr(criteria, "web_search_max_queries", self.settings.web_search_max_queries_per_run)
        max_results_per_q = getattr(criteria, "web_search_max_results_per_query", self.settings.web_search_max_results_per_query)
        max_candidate_pages = getattr(criteria, "web_search_max_candidate_pages", self.settings.web_search_max_candidate_pages_per_run)

        queries = self.query_generator.generate_queries(criteria, max_queries=max_queries)
        opportunities: list[OpportunityIn] = []

        seen_urls: set[str] = set()
        candidate_results: list[tuple[str, SearchResult]] = []

        # Step 1: Execute search queries and collect candidate search result links
        for query in queries:
            try:
                self.metrics.queries_executed += 1
                raw_results = await self.engine.search(query, limit=max_results_per_q)
                self.metrics.results_seen += len(raw_results)
                for res in raw_results:
                    clean_u = SearchResultFilter.clean_url(res.url)
                    if clean_u and clean_u not in seen_urls:
                        seen_urls.add(clean_u)
                        if SearchResultFilter.is_relevant_search_result(res):
                            candidate_results.append((query, res))
                await asyncio.sleep(0.3)
            except Exception as exc:
                self.metrics.errors.append(f"Query error '{query}': {exc}")

        self.metrics.candidate_urls_found = len(candidate_results)

        # Step 2: Fetch and extract job postings from candidate URLs
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }

        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            for query, res in candidate_results[:max_candidate_pages]:
                try:
                    url = res.url
                    domain = urlparse(url).netloc
                    self.metrics.discovered_domains.add(domain)

                    # Check for dedicated ATS handoff
                    ats_info = PageTypeDetector.detect_ats(url)
                    if ats_info and ats_info.get("ats") in ("greenhouse", "lever"):
                        opp = self._build_ats_handoff_opportunity(ats_info, res, query)
                        if opp:
                            opportunities.append(opp)
                            self.metrics.opportunities_extracted += 1
                            continue

                    # Fetch public web page
                    self.metrics.pages_fetched += 1
                    resp = await client.get(url, headers=headers)
                    if resp.status_code != 200:
                        continue
                    content_type = resp.headers.get("content-type", "")
                    if "text/html" not in content_type and "application/xhtml" not in content_type:
                        continue

                    extracted = JobPostingExtractor.extract_from_html(resp.text, url)
                    if not extracted or not extracted.get("title"):
                        continue

                    opp = self._normalize_extracted_opportunity(extracted, res, query)
                    if opp:
                        opportunities.append(opp)
                        self.metrics.opportunities_extracted += 1

                except Exception as exc:
                    self.metrics.errors.append(f"Fetch error '{res.url}': {exc}")

        return opportunities

    def _build_ats_handoff_opportunity(
        self, ats_info: dict, res: SearchResult, query: str
    ) -> OpportunityIn | None:
        ats = ats_info["ats"]
        title = res.title or "ATS Vacancy"
        if ats == "greenhouse":
            board = ats_info.get("board", "unknown")
            ext_id = ats_info.get("external_id")
            source = f"greenhouse:{board}"
            url = f"https://boards.greenhouse.io/{board}/jobs/{ext_id}" if ext_id else res.url
        elif ats == "lever":
            site = ats_info.get("site", "unknown")
            ext_id = ats_info.get("external_id")
            source = f"lever:{site}"
            url = f"https://jobs.lever.co/{site}/{ext_id}" if ext_id else res.url
        else:
            source = f"web_search:{ats}"
            url = res.url

        notes_parts = [
            "discovery_method=web_search",
            f"search_engine={self.engine.name}",
            f"search_query={query}",
            f"search_result_url={res.url}",
            f"original_source={ats}",
        ]

        return OpportunityIn(
            source=source,
            external_id=ats_info.get("external_id"),
            title=title,
            company=ats_info.get("board") or ats_info.get("site") or "Unknown company",
            url=url,
            location=None,
            description=res.snippet,
            posted_at=None,
            notes="; ".join(notes_parts),
        )

    def _normalize_extracted_opportunity(
        self, extracted: dict[str, Any], res: SearchResult, query: str
    ) -> OpportunityIn | None:
        title = extracted.get("title")
        if not title:
            return None

        url = extracted.get("canonical_url") or res.url
        domain = urlparse(url).netloc.replace("www.", "")

        posted_at_raw = extracted.get("posted_at")
        posted_at_dt = None
        if posted_at_raw:
            try:
                posted_at_dt = datetime.fromisoformat(str(posted_at_raw).replace("Z", "+00:00")).astimezone(timezone.utc)
            except Exception:
                pass

        notes_parts = [
            "discovery_method=web_search",
            f"search_engine={self.engine.name}",
            f"search_query={query}",
            f"search_result_url={res.url}",
            f"original_source={domain}",
            f"extraction_method={extracted.get('extraction_method', 'unknown')}",
        ]

        return OpportunityIn(
            source=f"web_search:{domain[:50]}",
            external_id=None,
            title=str(title).strip(),
            company=str(extracted.get("company") or domain).strip(),
            url=url,
            location=extracted.get("location"),
            description=extracted.get("description") or res.snippet,
            posted_at=posted_at_dt,
            notes="; ".join(notes_parts),
        )
