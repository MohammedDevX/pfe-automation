# PFE Job Application Automation System

A personal automation system for PFE (final-year internship) discovery, research, messaging, and application tracking.

> **Safety-first**: Email sending and ATS submissions are blocked by default (`DRY_RUN_EMAIL=true`, `DRY_RUN_ATS=true`). You must explicitly disable each guard in your `.env` when you are ready to act.

---

## What It Does

| Phase | What Happens |
|---|---|
| **Discovery** | Search Adzuna, Greenhouse, Lever, or add manually |
| **Scoring** | PFE-relevance scoring (0-100) with reasons |
| **Company Research** | Discover website, LinkedIn, contacts, emails |
| **Message Generation** | Template or GPT-4o-mini email + LinkedIn message |
| **Human Review** | Approve / reject / edit via `/ui` |
| **Email Sending** | SMTP delivery (blocked in dry-run mode) |
| **Response Tracking** | IMAP scanning + manual recording |
| **Follow-ups** | Automatic scheduling + generation |
| **ATS Application** | Greenhouse / Lever API (blocked in dry-run mode) |
| **Export** | Excel `.xlsx` with full application history |

No CAPTCHA solving, rate-limit bypassing, LinkedIn automation, or credential scraping.

---

## Quick Start

### 1. Install Requirements

```powershell
python -m venv .venv
.venv\Scripts\Activate
pip install -r requirements.txt
```

### 2. Configure `.env`

```powershell
copy .env.example .env
# Edit .env with your real values
```

Minimum required settings:

```env
CANDIDATE_FIRST_NAME=Your Name
CANDIDATE_LAST_NAME=Your Surname
CANDIDATE_EMAIL=you@university.edu
CANDIDATE_CV_PATH=C:\Users\you\Documents\cv.pdf
```

### 3. Start the API

```powershell
uvicorn app.main:app --reload
```

### 4. Run the Database Migration

The SQLite database is created automatically on first start.
If you are upgrading an existing database, run:

```powershell
.venv\Scripts\python -c "
from app.database import Base, engine
Base.metadata.create_all(bind=engine)
print('Done.')
"
```

### 5. Run Tests

```powershell
.venv\Scripts\python -m pytest tests\ -v
```

Expected: **106+ tests passing**.

### 6. Open the Review UI

```
http://localhost:8000/ui
```

API documentation:

```
http://localhost:8000/docs
```

---

## Candidate Profile

**Single source of truth: your `.env` file.**

All candidate information flows from environment variables into:
- Message templates (email + LinkedIn body)
- ATS payload preparation (Greenhouse, Lever)
- Application tracking

No information is duplicated or hardcoded. To update your profile, edit `.env` and restart.

| Setting | Description |
|---|---|
| `CANDIDATE_FIRST_NAME` | First name |
| `CANDIDATE_LAST_NAME` | Last name |
| `CANDIDATE_EMAIL` | Your email (also the sender for SMTP) |
| `CANDIDATE_PHONE` | Phone number |
| `CANDIDATE_LOCATION` | City, Country |
| `CANDIDATE_SCHOOL` | University / school name |
| `CANDIDATE_DEGREE` | Degree type (Master, Bachelor, …) |
| `CANDIDATE_SPECIALIZATION` | Your specialty |
| `CANDIDATE_SKILLS` | Comma-separated skills |
| `CANDIDATE_LINKEDIN_URL` | LinkedIn profile URL |
| `CANDIDATE_GITHUB_URL` | GitHub profile URL |
| `CANDIDATE_CV_PATH` | **Absolute path** to your CV file |
| `CANDIDATE_CV_SUMMARY` | 2-3 sentence summary for AI generation |

---

## CV Handling

Before any ATS submission the system validates:
- File exists at `CANDIDATE_CV_PATH`
- Format is `.pdf`, `.doc`, or `.docx`
- File is not empty and under 10 MB
- Path is absolute

If validation fails you receive a clear error like:

```
Cannot prepare application: CV file not found.
Expected file at: C:\Users\you\cv.pdf
Make sure the file exists and CANDIDATE_CV_PATH points to it.
```

The CV is **never uploaded during a dry run**.

---

## Dry-Run Workflow

Both guards default to `True` (safe). The complete dry-run workflow:

```
POST /opportunities/ingest         → add a job
POST /opportunities/{id}/score     → score it
POST /opportunities/{id}/research  → company + contacts
POST /applications/{id}/messages/generate  → write email + LinkedIn
GET  /ui/messages/{id}             → review draft
POST /applications/{id}/prepare    → validate CV, detect ATS, build payload
```

At `/prepare` you can see:
- Detected ATS provider (Greenhouse, Lever, or Manual)
- Candidate fields mapped to ATS questions
- Any missing required fields
- Whether the payload is ready

`POST /applications/{id}/submit` will be blocked with a clear dry-run message until you set `DRY_RUN_ATS=false`.

---

## Safety Flags

| Flag | Default | Effect when `true` |
|---|---|---|
| `DRY_RUN_EMAIL` | `true` | Blocks SMTP delivery; email body is stored but not sent |
| `DRY_RUN_ATS` | `true` | Blocks ATS API submission; payload is prepared but not submitted |

To enable real sends, set either flag to `false` in `.env`. **Both must be disabled independently.** This design ensures you cannot accidentally send email while testing ATS, or vice versa.

---

## Workflow Examples

### Ingest a Job Manually

```powershell
Invoke-RestMethod -Method Post http://localhost:8000/opportunities/ingest `
  -ContentType "application/json" `
  -Body (@{
    source = "manual"
    title = "PFE Backend Developer"
    company = "ExampleCo"
    location = "Casablanca"
    url = "https://example.com/jobs/pfe-backend"
    description = "Python FastAPI PostgreSQL internship."
  } | ConvertTo-Json)
```

### Search Adzuna

```powershell
Invoke-RestMethod -Method Post http://localhost:8000/opportunities/search `
  -ContentType "application/json" `
  -Body (@{
    keywords  = @("PFE", "stage", "backend")
    locations = @("Casablanca", "Rabat")
    providers = @("adzuna")
  } | ConvertTo-Json)
```

### Research a Company

```powershell
Invoke-RestMethod -Method Post http://localhost:8000/opportunities/1/research `
  -ContentType "application/json" `
  -Body (@{
    providers = @("public_website", "hunter")
    website   = "https://example.com"
  } | ConvertTo-Json)
```

### Prepare an ATS Application (Dry Run)

```powershell
Invoke-RestMethod -Method Post http://localhost:8000/applications/1/prepare
```

### Export to Excel

```powershell
Invoke-WebRequest http://localhost:8000/export/applications.xlsx -OutFile applications.xlsx
```

---

## Docker

```powershell
docker compose up --build
```

---

## External Services

| Service | Env Var | Required | Notes |
|---|---|---|---|
| Adzuna | `ADZUNA_APP_ID` / `ADZUNA_APP_KEY` | Optional | Job discovery |
| Hunter.io | `HUNTER_API_KEY` | Optional | Email discovery |
| OpenAI | `OPENAI_API_KEY` | Optional | AI message generation |
| Gmail SMTP | `SMTP_USER` / `SMTP_PASSWORD` | Optional | Email sending |
| Gmail IMAP | Uses SMTP creds | Optional | Reply detection |

All services degrade gracefully when credentials are missing.

---

## Architecture

```
app/
  database.py          Settings (all config, single source of truth)
  models.py            SQLAlchemy ORM models
  schemas.py           Pydantic request/response schemas
  scoring.py           PFE relevance scoring
  main.py              FastAPI routes + minimal HTML review UI

  integrations/
    application_providers.py   Greenhouse / Lever / Manual ATS abstractions
    cv_validator.py            CV file validation (no upload)
    email_senders.py           SMTP / DryRun / Null email senders
    message_providers.py       Template / OpenAI message generation
    research_providers.py      Public website / Hunter contact discovery
    imap_reader.py             IMAP inbox scanning

  services/
    application.py    ATS submission flow (prepare + submit)
    discovery.py      Job search and persistence
    export.py         Excel export
    lifecycle.py      Status transitions, follow-ups, response tracking
    messaging.py      Message generation, review, sending
    opportunities.py  Opportunity CRUD helpers
    research.py       Company + contact research orchestration

tests/                 106+ unit + integration tests
docs/                  Architecture and platform audit docs
```
