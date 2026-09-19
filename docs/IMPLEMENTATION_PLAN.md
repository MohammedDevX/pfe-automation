# Implementation Plan

## Proposed Architecture

Build vertically around a single API service and one database. The API owns ingestion, deduplication, scoring, review state, contact/application events, and exports. Workflow orchestration with n8n can be added once the data model is proven.

## Selected Technologies

- Python + FastAPI for the backend
- SQLAlchemy for persistence
- PostgreSQL as the primary database
- SQLite fallback for development
- httpx for official/public feed integrations
- openpyxl for Excel export
- Docker Compose for repeatable startup

## Platform Integration Notes

- LinkedIn: candidate-facing job discovery/application APIs are restricted Talent Solutions partner APIs. Current public docs focus on employers/ATS partners posting jobs and Apply Connect, not a general candidate search/apply API. Do not scrape or automate protected LinkedIn workflows. Keep LinkedIn actions as manually reviewed links/contact notes unless the user has an approved integration.
- Indeed: do not assume candidate API access. Use permitted aggregator APIs or manual import until an official workflow is confirmed for the user's account/use case.
- Greenhouse: public Job Board API exposes published jobs without auth; application submission requires authenticated server-side use and is better deferred behind human approval.
- Lever: public Postings API exposes one company's public jobs by known site slug; application submission needs site/application config and should usually redirect to hosted forms unless explicitly supported.
- Aggregators: Adzuna and Jooble provide documented job search APIs that can speed discovery with API keys.
- Email discovery: Hunter provides documented Domain Search, Email Finder, and Email Verifier APIs with API key authentication, confidence, verification status, and test-key support. It is kept behind a provider abstraction.

Sources checked on 2026-09-02:

- https://docs.greenhouse.io/job-board.html
- https://github.com/lever/postings-api
- https://developer.adzuna.com/overview
- https://help.jooble.org/en/support/solutions/articles/60001448238-rest-api-documentation
- https://learn.microsoft.com/en-us/linkedin/talent/apply-connect/create-apply-connect-jobs
- https://help.hunter.io/en/articles/1970956-hunter-api

## MVP Scope

The MVP is a reviewable opportunity pipeline:

1. Search Adzuna, Lever, or Greenhouse providers where configured.
2. Ingest one opportunity from manual JSON, Lever, or Greenhouse.
3. Normalize and deduplicate it.
4. Score it for PFE relevance.
5. Store it in the database.
6. Present pending opportunities for review.
7. Allow approve/reject/edit.
8. Track timeline events.
9. Export the central record to Excel.

## First Vertical Slice

`search criteria -> provider fetch -> normalized opportunities -> deduplication -> qualification score -> database -> filtered retrieval -> review -> tracking -> Excel export`

Outbound email, LinkedIn messaging, browser submission, recruiter discovery, and OpenAI-generated messages are intentionally deferred until the approval model and central record are working.

## Project Structure

```text
app/
  main.py
  database.py
  models.py
  schemas.py
  scoring.py
  integrations/
    ats.py
    providers.py
  services/
    discovery.py
    opportunities.py
    export.py
tests/
  test_opportunities.py
  test_scoring.py
docker-compose.yml
Dockerfile
requirements.txt
```

## Development Steps

1. Create API, database models, and migrations-free startup for MVP speed.
2. Add manual, Lever, and Greenhouse ingestion.
3. Add Adzuna-backed provider search when credentials are configured.
4. Add deterministic scoring so the MVP works without paid AI keys.
5. Add review and event endpoints.
6. Add Excel export.
7. Run through Docker once Docker is available.
