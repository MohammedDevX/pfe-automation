import re

import httpx

from app.schemas import OpportunityIn


TAG_RE = re.compile(r"<[^>]+>")


def clean_html(value: str | None) -> str | None:
    if not value:
        return None
    return TAG_RE.sub(" ", value).replace("&nbsp;", " ").strip()


async def fetch_lever(site: str) -> list[OpportunityIn]:
    url = f"https://api.lever.co/v0/postings/{site}?mode=json"
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(url, headers={"Accept": "application/json"})
        response.raise_for_status()
    items = response.json()
    opportunities: list[OpportunityIn] = []
    for item in items:
        categories = item.get("categories") or {}
        opportunities.append(
            OpportunityIn(
                source=f"lever:{site}",
                external_id=item.get("id"),
                title=item.get("text") or "Untitled",
                company=site,
                url=item.get("hostedUrl") or item.get("applyUrl"),
                location=categories.get("location"),
                description=clean_html(item.get("descriptionPlain") or item.get("description")),
            )
        )
    return opportunities


async def fetch_greenhouse(board_token: str) -> list[OpportunityIn]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs"
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(url, params={"content": "true"})
        response.raise_for_status()
    jobs = response.json().get("jobs", [])
    opportunities: list[OpportunityIn] = []
    for job in jobs:
        location = (job.get("location") or {}).get("name")
        opportunities.append(
            OpportunityIn(
                source=f"greenhouse:{board_token}",
                external_id=str(job.get("id")),
                title=job.get("title") or "Untitled",
                company=board_token,
                url=job.get("absolute_url"),
                location=location,
                description=clean_html(job.get("content")),
            )
        )
    return opportunities
