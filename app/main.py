import re
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, Response
from fastapi.responses import HTMLResponse
from sqlalchemy import inspect, or_, select, text
from sqlalchemy.orm import Session

from app.database import Base, engine, get_db, get_settings
from app.integrations.ats import fetch_greenhouse, fetch_lever
from app.integrations.imap_reader import fetch_replies
from app.models import (
    Application, ApplicationStatus, Company, Contact,
    DiscoveryRun, DiscoveryRunStatus, IncomingResponse,
    MessageStatus, OutboundMessage, ProfessionalEmail,
    ResponseStatus, ReviewStatus,
)
from app.schemas import (
    ApplicationOut,
    ApplicationPatch,
    ConfirmResponseRequest,
    EventCreate,
    EventOut,
    FollowUpDueItem,
    IncomingResponseOut,
    ApplicationPayloadOut,
    ApplicationSubmitRequest,
    IngestResult,
    MessageGenerateRequest,
    MessageOut,
    MessagePatch,
    MessageSendRequest,
    NotionConnectionStatus,
    NotionSyncResult,
    OpportunityIn,
    CompanyOut,
    ContactOut,
    ProfessionalEmailOut,
    RecordResponseRequest,
    ResearchRequest,
    ResearchResult,
    ReviewUpdate,
    SearchCriteria,
    DiscoveryResult,
    DiscoveryRunOut,
    StatusTransitionRequest,
)
from app.services.application import prepare_application_submission, submit_application
from app.services.discovery import search_and_persist
from app.services.scheduler import scheduler_manager
from app.services.export import applications_xlsx
from app.services.lifecycle import (
    confirm_response,
    get_awaiting_response,
    get_due_follow_ups,
    mark_follow_up_sent,
    mark_follow_ups_due,
    record_response,
    schedule_follow_up,
    transition_status,
)
from app.services.messaging import (
    approve_message,
    edit_message,
    generate_message,
    reject_message,
    send_message,
)
from app.services.opportunities import add_event, upsert_opportunity
from app.services.research import research_application
from app.services.notion_sync import import_from_notion

@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    ensure_additive_columns()
    scheduler_manager.start(get_settings())
    yield
    scheduler_manager.stop()


app = FastAPI(title="PFE Job Application Automation", version="0.1.0", lifespan=lifespan)


def ensure_additive_columns() -> None:
    """Add columns that were introduced after the initial schema creation.

    SQLite does not support IF NOT EXISTS for ALTER TABLE, so we check first.
    PostgreSQL is permissive but we guard anyway to keep both dialects happy.
    """
    inspector = inspect(engine)
    existing_tables = inspector.get_table_names()
    if "discovery_runs" not in existing_tables:
        DiscoveryRun.__table__.create(bind=engine, checkfirst=True)
    if "applications" not in existing_tables:
        return  # create_all will build it; nothing to backfill
    application_columns = {column["name"] for column in inspector.get_columns("applications")}
    datetime_type = "TIMESTAMP" if engine.dialect.name == "postgresql" else "DATETIME"
    date_type = "DATE"
    with engine.begin() as connection:
        if "company_id" not in application_columns:
            connection.execute(text("ALTER TABLE applications ADD COLUMN company_id INTEGER"))
        if "posted_at" not in application_columns:
            connection.execute(text(f"ALTER TABLE applications ADD COLUMN posted_at {datetime_type}"))
        if "last_response_date" not in application_columns:
            connection.execute(text(f"ALTER TABLE applications ADD COLUMN last_response_date {date_type}"))
        if "follow_up_count" not in application_columns:
            connection.execute(text("ALTER TABLE applications ADD COLUMN follow_up_count INTEGER DEFAULT 0"))
        if "relevance_category" not in application_columns:
            connection.execute(text("ALTER TABLE applications ADD COLUMN relevance_category VARCHAR(50)"))
        if "notion_page_id" not in application_columns:
            # VARCHAR(100) — stores the Notion page UUID for matched/imported records.
            # Unique but nullable (SQLite allows multiple NULL values in a UNIQUE column).
            connection.execute(text("ALTER TABLE applications ADD COLUMN notion_page_id VARCHAR(100)"))


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/opportunities/ingest", response_model=ApplicationOut)
def ingest_opportunity(opportunity: OpportunityIn, db: Session = Depends(get_db)) -> Application:
    application, _ = upsert_opportunity(db, opportunity)
    db.commit()
    db.refresh(application)
    return application


@app.post("/opportunities/discover", response_model=DiscoveryResult)
async def discover_opportunities(
    criteria: SearchCriteria = SearchCriteria(),
    db: Session = Depends(get_db),
) -> DiscoveryResult:
    try:
        return await search_and_persist(db, criteria, get_settings())
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/discovery/run", response_model=DiscoveryRunOut)
async def trigger_discovery_run(
    criteria: SearchCriteria = SearchCriteria(),
    db: Session = Depends(get_db),
) -> DiscoveryRun:
    """Trigger a manual discovery run using the discovery scheduler manager."""
    run_rec = await scheduler_manager.trigger_manual_run(
        criteria=criteria,
        trigger_source="MANUAL",
        settings=get_settings(),
    )
    return run_rec


@app.get("/discovery/runs", response_model=list[DiscoveryRunOut])
def list_discovery_runs(
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> list[DiscoveryRun]:
    """List recent discovery runs with execution metrics."""
    query = select(DiscoveryRun).order_by(DiscoveryRun.started_at.desc()).limit(limit)
    return list(db.scalars(query))


@app.get("/discovery/runs/{run_id}", response_model=DiscoveryRunOut)
def get_discovery_run(
    run_id: str,
    db: Session = Depends(get_db),
) -> DiscoveryRun:
    """Get details of a specific discovery run."""
    run_rec = db.scalar(select(DiscoveryRun).where(DiscoveryRun.run_id == run_id))
    if not run_rec:
        raise HTTPException(status_code=404, detail=f"Discovery run {run_id} not found")
    return run_rec


@app.get("/opportunities/new", response_model=list[ApplicationOut])
def list_new_opportunities(
    min_score: int | None = Query(default=50, ge=0, le=100),
    db: Session = Depends(get_db),
) -> list[Application]:
    query = select(Application).where(Application.application_status == ApplicationStatus.discovered)
    if min_score is not None:
        query = query.where(Application.score >= min_score)
    query = query.order_by(Application.score.desc(), Application.created_at.desc())
    return list(db.scalars(query))


@app.get("/opportunities", response_model=list[ApplicationOut])
def list_opportunities(
    min_score: int | None = Query(default=None, ge=0, le=100),
    source: str | None = None,
    location: str | None = None,
    status: ReviewStatus | None = None,
    keywords: str | None = None,
    db: Session = Depends(get_db),
) -> list[Application]:
    query = select(Application)
    if min_score is not None:
        query = query.where(Application.score >= min_score)
    if source:
        query = query.where(Application.source.ilike(f"%{source}%"))
    if location:
        query = query.where(Application.location.ilike(f"%{location}%"))
    if status:
        query = query.where(Application.review_status == status)
    if keywords:
        terms = [term.strip() for term in keywords.split(",") if term.strip()]
        for term in terms:
            pattern = f"%{term}%"
            query = query.where(
                or_(
                    Application.position.ilike(pattern),
                    Application.company.ilike(pattern),
                    Application.description.ilike(pattern),
                    Application.notes.ilike(pattern),
                )
            )
    query = query.order_by(Application.score.desc(), Application.created_at.desc())
    return list(db.scalars(query))


@app.post("/opportunities/{opportunity_id}/research", response_model=ResearchResult)
async def research_opportunity(
    opportunity_id: int,
    request: ResearchRequest = ResearchRequest(),
    db: Session = Depends(get_db),
) -> ResearchResult:
    application = require_application(db, opportunity_id)
    return await research_application(db, application, request, get_settings())


@app.get("/companies", response_model=list[CompanyOut])
def list_companies(
    name: str | None = None,
    db: Session = Depends(get_db),
) -> list[Company]:
    query = select(Company).order_by(Company.name)
    if name:
        query = query.where(Company.normalized_name.ilike(f"%{name.lower()}%"))
    return list(db.scalars(query))


@app.get("/companies/{company_id}", response_model=CompanyOut)
def get_company(company_id: int, db: Session = Depends(get_db)) -> Company:
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    return company


@app.get("/companies/{company_id}/contacts", response_model=list[ContactOut])
def list_company_contacts(company_id: int, db: Session = Depends(get_db)) -> list[Contact]:
    get_company(company_id, db)
    return list(db.scalars(select(Contact).where(Contact.company_id == company_id).order_by(Contact.discovered_at.desc())))


@app.get("/companies/{company_id}/emails", response_model=list[ProfessionalEmailOut])
def list_company_emails(company_id: int, db: Session = Depends(get_db)) -> list[ProfessionalEmail]:
    get_company(company_id, db)
    return list(
        db.scalars(
            select(ProfessionalEmail)
            .where(ProfessionalEmail.company_id == company_id)
            .order_by(ProfessionalEmail.confidence.desc().nullslast(), ProfessionalEmail.discovered_at.desc())
        )
    )


@app.post("/sources/lever/{site}/ingest", response_model=IngestResult)
async def ingest_lever(site: str, db: Session = Depends(get_db)) -> IngestResult:
    return ingest_many(await fetch_lever(site), db)


@app.post("/sources/greenhouse/{board_token}/ingest", response_model=IngestResult)
async def ingest_greenhouse(board_token: str, db: Session = Depends(get_db)) -> IngestResult:
    return ingest_many(await fetch_greenhouse(board_token), db)


def ingest_many(opportunities: list[OpportunityIn], db: Session) -> IngestResult:
    created = 0
    updated = 0
    applications = []
    for opportunity in opportunities:
        application, was_created = upsert_opportunity(db, opportunity)
        created += int(was_created)
        updated += int(not was_created)
        applications.append(application)
    db.commit()
    for application in applications:
        db.refresh(application)
    return IngestResult(created=created, updated=updated, applications=applications)


@app.get("/applications", response_model=list[ApplicationOut])
def list_applications(
    review_status: ReviewStatus | None = None,
    db: Session = Depends(get_db),
) -> list[Application]:
    query = select(Application).order_by(Application.score.desc(), Application.created_at.desc())
    if review_status:
        query = query.where(Application.review_status == review_status)
    return list(db.scalars(query))


@app.get("/applications/{application_id}", response_model=ApplicationOut)
def get_application(application_id: int, db: Session = Depends(get_db)) -> Application:
    return require_application(db, application_id)


@app.patch("/applications/{application_id}", response_model=ApplicationOut)
def update_application(
    application_id: int,
    patch: ApplicationPatch,
    db: Session = Depends(get_db),
) -> Application:
    application = require_application(db, application_id)
    for key, value in patch.model_dump(exclude_unset=True).items():
        if value is not None and key.endswith("_url"):
            value = str(value)
        setattr(application, key, value)
    db.commit()
    db.refresh(application)
    return application


@app.post("/applications/{application_id}/review", response_model=ApplicationOut)
def review_application(
    application_id: int,
    review: ReviewUpdate,
    db: Session = Depends(get_db),
) -> Application:
    application = require_application(db, application_id)
    application.review_status = review.review_status
    if review.notes:
        application.notes = review.notes
    if review.generated_message:
        application.generated_message = review.generated_message
    add_event(
        db,
        application,
        EventCreate(
            event_type=f"review_{review.review_status.value}",
            channel="human_review",
            notes=review.notes,
        ),
    )
    db.commit()
    db.refresh(application)
    return application


@app.post("/applications/{application_id}/events", response_model=EventOut)
def create_event(
    application_id: int,
    event: EventCreate,
    db: Session = Depends(get_db),
):
    application = require_application(db, application_id)
    model = add_event(db, application, event)
    db.commit()
    db.refresh(model)
    return model


@app.get("/applications/{application_id}/events", response_model=list[EventOut])
def list_events(application_id: int, db: Session = Depends(get_db)):
    application = require_application(db, application_id)
    return application.events


@app.get("/export/applications.xlsx")
def export_applications(db: Session = Depends(get_db)) -> Response:
    output = applications_xlsx(db)
    return Response(
        output.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=applications.xlsx"},
    )


# ===========================================================================
# Lifecycle / status endpoints
# ===========================================================================


@app.post("/applications/{application_id}/status", response_model=ApplicationOut)
def update_application_status(
    application_id: int,
    body: StatusTransitionRequest,
    db: Session = Depends(get_db),
) -> Application:
    application = require_application(db, application_id)
    try:
        return transition_status(db, application, body.status, body.notes)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/applications/{application_id}/prepare", response_model=ApplicationPayloadOut)
async def prepare_application_endpoint(
    application_id: int,
    db: Session = Depends(get_db),
):
    application = require_application(db, application_id)
    try:
        return await prepare_application_submission(db, application, get_settings())
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/applications/{application_id}/submit", response_model=ApplicationOut)
async def submit_application_endpoint(
    application_id: int,
    body: ApplicationSubmitRequest,
    db: Session = Depends(get_db),
):
    application = require_application(db, application_id)
    try:
        return await submit_application(
            db, 
            application, 
            get_settings(), 
            answers_override=body.answers_override,
            force_resubmit=body.force_resubmit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/applications/{application_id}/follow-up/schedule", response_model=ApplicationOut)
def schedule_application_follow_up(
    application_id: int,
    db: Session = Depends(get_db),
) -> Application:
    application = require_application(db, application_id)
    try:
        return schedule_follow_up(db, application, get_settings())
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/applications/{application_id}/follow-up/sent", response_model=ApplicationOut)
def mark_application_follow_up_sent(
    application_id: int,
    db: Session = Depends(get_db),
) -> Application:
    return mark_follow_up_sent(db, require_application(db, application_id))


# ===========================================================================
# Response ingestion endpoints
# ===========================================================================


@app.post("/applications/{application_id}/responses", response_model=IncomingResponseOut)
def record_application_response(
    application_id: int,
    body: RecordResponseRequest,
    db: Session = Depends(get_db),
) -> IncomingResponse:
    application = require_application(db, application_id)
    return record_response(db, application, body)


@app.get("/applications/{application_id}/responses", response_model=list[IncomingResponseOut])
def list_application_responses(
    application_id: int,
    db: Session = Depends(get_db),
) -> list[IncomingResponse]:
    require_application(db, application_id)
    return list(db.scalars(
        select(IncomingResponse)
        .where(IncomingResponse.application_id == application_id)
        .order_by(IncomingResponse.received_at.desc())
    ))


@app.post("/responses/{response_id}/confirm", response_model=IncomingResponseOut)
def confirm_application_response(
    response_id: int,
    body: ConfirmResponseRequest,
    db: Session = Depends(get_db),
) -> IncomingResponse:
    resp = db.get(IncomingResponse, response_id)
    if not resp:
        raise HTTPException(status_code=404, detail="Response not found")
    return confirm_response(db, resp, body.classification)


# ===========================================================================
# IMAP inbox scan endpoint
# ===========================================================================


@app.post("/inbox/scan")
def scan_inbox(
    since_days: int = Query(default=30, ge=1, le=90),
    db: Session = Depends(get_db),
) -> dict:
    """Scan the configured IMAP inbox for potential replies and return candidates.

    Does NOT persist anything automatically — caller reviews and records via
    POST /applications/{id}/responses.
    """
    s = get_settings()
    try:
        candidates = fetch_replies(
            imap_host=s.imap_host,
            imap_port=s.imap_port,
            username=s.smtp_user or "",
            password=s.smtp_password or "",
            since_days=since_days,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "count": len(candidates),
        "candidates": [
            {
                "message_id": c.message_id,
                "sender": c.sender,
                "subject": c.subject,
                "received_at": c.received_at.isoformat(),
                "classification_hint": c.classification_hint,
                "confidence": c.confidence,
                "body_preview": c.body_preview[:200],
            }
            for c in candidates
        ],
    }


# ===========================================================================
# Follow-up dashboard endpoints
# ===========================================================================


@app.get("/follow-ups/due", response_model=list[FollowUpDueItem])
def list_due_follow_ups(db: Session = Depends(get_db)) -> list[Application]:
    """Return applications with a follow-up due today or overdue."""
    return get_due_follow_ups(db)


@app.post("/follow-ups/mark-due")
def trigger_mark_follow_ups_due(db: Session = Depends(get_db)) -> dict:
    """Scan waiting_response applications and flag overdue ones as follow_up_due."""
    count = mark_follow_ups_due(db)
    return {"updated": count}


@app.get("/applications/awaiting-response", response_model=list[ApplicationOut])
def list_awaiting_response(db: Session = Depends(get_db)) -> list[Application]:
    """Return applications in waiting_response with no follow-up due yet."""
    return get_awaiting_response(db)



def require_application(db: Session, application_id: int) -> Application:
    application = db.get(Application, application_id)
    if not application:
        raise HTTPException(status_code=404, detail="Application not found")
    return application


def require_message(db: Session, message_id: int) -> OutboundMessage:
    message = db.get(OutboundMessage, message_id)
    if not message:
        raise HTTPException(status_code=404, detail="Message not found")
    return message


# ===========================================================================
# Message endpoints
# ===========================================================================


@app.post("/applications/{application_id}/messages/generate", response_model=MessageOut)
async def generate_opportunity_message(
    application_id: int,
    request: MessageGenerateRequest = MessageGenerateRequest(),
    db: Session = Depends(get_db),
) -> OutboundMessage:
    application = require_application(db, application_id)
    try:
        return await generate_message(db, application, request, get_settings())
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/applications/{application_id}/messages", response_model=list[MessageOut])
def list_application_messages(
    application_id: int,
    db: Session = Depends(get_db),
) -> list[OutboundMessage]:
    require_application(db, application_id)
    return list(
        db.scalars(
            select(OutboundMessage)
            .where(OutboundMessage.application_id == application_id)
            .order_by(OutboundMessage.created_at.desc())
        )
    )


@app.get("/messages/{message_id}", response_model=MessageOut)
def get_message(message_id: int, db: Session = Depends(get_db)) -> OutboundMessage:
    return require_message(db, message_id)


@app.patch("/messages/{message_id}", response_model=MessageOut)
def patch_message(
    message_id: int,
    patch: MessagePatch,
    db: Session = Depends(get_db),
) -> OutboundMessage:
    message = require_message(db, message_id)
    if message.status in (MessageStatus.sent, MessageStatus.rejected):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot edit a {message.status.value} message.",
        )
    return edit_message(db, message, patch)


@app.post("/messages/{message_id}/approve", response_model=MessageOut)
def approve_opportunity_message(
    message_id: int,
    db: Session = Depends(get_db),
) -> OutboundMessage:
    message = require_message(db, message_id)
    try:
        return approve_message(db, message)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/messages/{message_id}/reject", response_model=MessageOut)
def reject_opportunity_message(
    message_id: int,
    db: Session = Depends(get_db),
) -> OutboundMessage:
    message = require_message(db, message_id)
    try:
        return reject_message(db, message)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/messages/{message_id}/send", response_model=MessageOut)
async def send_opportunity_message(
    message_id: int,
    request: MessageSendRequest = MessageSendRequest(),
    db: Session = Depends(get_db),
) -> OutboundMessage:
    message = require_message(db, message_id)
    try:
        return await send_message(db, message, get_settings(), to_email=request.to_email)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ===========================================================================
# Review UI  (minimal HTML — no framework, no polish required)
# ===========================================================================


@app.get("/ui", response_class=HTMLResponse, include_in_schema=False)
def ui_index(db: Session = Depends(get_db)) -> str:
    messages = list(
        db.scalars(
            select(OutboundMessage)
            .where(OutboundMessage.status.in_([MessageStatus.draft, MessageStatus.approved]))
            .order_by(OutboundMessage.created_at.desc())
        )
    )
    rows = ""
    for m in messages:
        app_obj = db.get(Application, m.application_id)
        badge = {
            "draft": "🟡 Draft",
            "approved": "🟢 Approved",
        }.get(m.status.value, m.status.value)
        rows += (
            f"<tr>"
            f"<td><a href='/ui/messages/{m.id}'>{m.id}</a></td>"
            f"<td>{app_obj.company if app_obj else '?'}</td>"
            f"<td>{app_obj.position if app_obj else '?'}</td>"
            f"<td>{m.channel.value}</td>"
            f"<td>{badge}</td>"
            f"<td>{m.created_at.strftime('%Y-%m-%d %H:%M') if m.created_at else ''}</td>"
            f"<td><a href='/ui/messages/{m.id}'>Review →</a></td>"
            f"</tr>"
        )
    if not rows:
        rows = "<tr><td colspan='7' style='text-align:center;color:#888'>No pending messages</td></tr>"

    apps = list(db.scalars(
        select(Application)
        .where(Application.application_status.in_([ApplicationStatus.discovered, ApplicationStatus.qualified, ApplicationStatus.researched, ApplicationStatus.contact_ready, ApplicationStatus.contacted]))
        .order_by(Application.created_at.desc())
    ))
    app_rows = ""
    for a in apps:
        rev_badge = "🟢 Reviewed" if a.review_status == ReviewStatus.approved else ("🔴 Skipped" if a.review_status == ReviewStatus.rejected else "🟡 Pending")
        app_rows += (
            f"<tr>"
            f"<td><a href='/ui/applications/{a.id}/apply'>{a.id}</a></td>"
            f"<td>{a.company}</td>"
            f"<td>{a.position}</td>"
            f"<td>{a.application_status.value}</td>"
            f"<td>{rev_badge}</td>"
            f"<td>"
            f"<a href='/ui/applications/{a.id}/apply' style='margin-right:6px'>Prepare / Apply →</a>"
            f"<button onclick='setReview({a.id}, \"approved\")' style='font-size:0.75rem;padding:2px 6px;margin-right:3px;cursor:pointer;'>Mark Reviewed</button>"
            f"<button onclick='setReview({a.id}, \"rejected\")' style='font-size:0.75rem;padding:2px 6px;cursor:pointer;'>Skip</button>"
            f"</td>"
            f"</tr>"
        )
    if not app_rows:
        app_rows = "<tr><td colspan='6' style='text-align:center;color:#888'>No applications found</td></tr>"

    return f"""<!DOCTYPE html><html lang='fr'><head><meta charset='UTF-8'>
<title>PFE — Application Dashboard</title>
<style>body{{font-family:system-ui,sans-serif;max-width:960px;margin:2rem auto;padding:0 1rem}}
table{{width:100%;border-collapse:collapse;margin-bottom:2rem}}th,td{{text-align:left;padding:.5rem .75rem;border-bottom:1px solid #e0e0e0}}
th{{background:#f5f5f5}}a{{color:#1a73e8}}h1,h2{{margin-bottom:1rem}}</style></head>
<body><h1>📋 PFE Dashboard</h1>
<p><a href='/docs'>API docs</a> | <a href='/ui/follow-ups'>📅 Follow-up Dashboard</a> | <a href='/ui/discoveries'>🔍 New Discoveries</a></p>
<h2>Pending Messages</h2>
<table><thead><tr><th>#</th><th>Company</th><th>Position</th><th>Channel</th>
<th>Status</th><th>Created</th><th>Action</th></tr></thead>
<tbody>{rows}</tbody></table>
<h2>Applications</h2>
<table><thead><tr><th>#</th><th>Company</th><th>Position</th><th>Status</th><th>Review Status</th><th>Action</th></tr></thead>
<tbody>{app_rows}</tbody></table>
<script>
async function setReview(id, status) {{
  await fetch('/applications/' + id + '/review', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{review_status: status}})
  }});
  location.reload();
}}
</script>
</body></html>"""


def _get_category_badge(cat: str | None) -> str:
    if not cat:
        return ""
    cat_upper = cat.upper()
    if cat_upper == "EXPLICIT_PFE":
        return "<span style='background:#d4edda;color:#155724;padding:2px 6px;border-radius:4px;font-size:0.75rem;font-weight:bold;'>🎓 PFE</span> "
    elif cat_upper == "EXPLICIT_INTERNSHIP":
        return "<span style='background:#cce5ff;color:#004085;padding:2px 6px;border-radius:4px;font-size:0.75rem;font-weight:bold;'>💼 Internship</span> "
    elif cat_upper == "GRADUATE":
        return "<span style='background:#e2e3e5;color:#383d41;padding:2px 6px;border-radius:4px;font-size:0.75rem;font-weight:bold;'>🎓 Graduate</span> "
    elif cat_upper == "JUNIOR":
        return "<span style='background:#fff3cd;color:#856404;padding:2px 6px;border-radius:4px;font-size:0.75rem;font-weight:bold;'>🟡 Junior</span> "
    elif cat_upper == "FULL_TIME":
        return "<span style='background:#f8f9fa;color:#6c757d;padding:2px 6px;border-radius:4px;font-size:0.75rem;font-weight:bold;'>⚪ Full-time</span> "
    elif cat_upper == "SENIOR":
        return "<span style='background:#f8d7da;color:#721c24;padding:2px 6px;border-radius:4px;font-size:0.75rem;font-weight:bold;'>🔴 Senior</span> "
    return f"<span style='background:#eee;color:#333;padding:2px 6px;border-radius:4px;font-size:0.75rem;'>{cat}</span> "


@app.get("/ui/discoveries", response_class=HTMLResponse, include_in_schema=False)
def ui_discoveries(
    min_score: int = Query(default=50, ge=0, le=100),
    db: Session = Depends(get_db),
) -> str:
    """UI view for newly discovered relevant opportunities."""
    settings = get_settings()
    recent_runs = list(db.scalars(select(DiscoveryRun).order_by(DiscoveryRun.started_at.desc()).limit(5)))

    run_rows = ""
    for r in recent_runs:
        dur_str = f"{(r.finished_at - r.started_at).seconds}s" if r.finished_at and r.started_at else "—"
        badge_color = "#28a745" if r.status == DiscoveryRunStatus.completed else ("#ffc107" if r.status == DiscoveryRunStatus.partial else "#dc3545")
        run_rows += (
            f"<tr>"
            f"<td><code>{r.run_id}</code></td>"
            f"<td><span style='background:{badge_color};color:#fff;padding:2px 6px;border-radius:4px;font-size:0.75rem;font-weight:bold;'>{r.status.value.upper()}</span></td>"
            f"<td>{r.started_at.strftime('%Y-%m-%d %H:%M') if r.started_at else '—'}</td>"
            f"<td>{r.jobs_fetched}</td>"
            f"<td>{r.new_opportunities}</td>"
            f"<td><b>{r.new_high_value_opportunities}</b></td>"
            f"<td>{dur_str}</td>"
            f"</tr>"
        )
    if not run_rows:
        run_rows = "<tr><td colspan='7' style='text-align:center;color:#888'>No discovery runs recorded yet</td></tr>"

    query = (
        select(Application)
        .where(Application.application_status == ApplicationStatus.discovered)
        .where(Application.score >= min_score)
        .order_by(Application.score.desc(), Application.created_at.desc())
    )
    apps = list(db.scalars(query))
    rows = ""
    for a in apps:
        loc = a.location or "—"
        reason_short = (a.score_reason or "")[:120]
        url_link = f"<a href='{a.job_url}' target='_blank'>🔗</a>" if a.job_url and "notion.so" not in a.job_url.lower() else "—"
        date_str = a.created_at.strftime('%Y-%m-%d %H:%M') if a.created_at else "—"
        warning_badge = ""
        if (a.notes and "Visa/Auth Warning" in a.notes) or (a.score_reason and "Visa/Auth Warning" in a.score_reason):
            warning_badge = "<br><span style='color:#c9302c;font-weight:bold;'>⚠️ Visa/Auth Warning</span>"

        cat_badge = _get_category_badge(a.relevance_category)
        source_display = a.source
        if a.source and a.source.startswith("web_search"):
            source_display = f"<span style='background:#e0f7fa;color:#006064;padding:2px 6px;border-radius:4px;font-size:0.75rem;font-weight:bold;'>🌐 {a.source}</span>"

        query_info = ""
        if a.notes and "search_query=" in a.notes:
            m_q = re.search(r'search_query=([^;]+)', a.notes)
            if m_q:
                query_info = f"<br><span style='font-size:.7rem;color:#00695c;'>🔍 Query: <i>{m_q.group(1)}</i></span>"

        rev_badge = "🟢 Reviewed" if a.review_status == ReviewStatus.approved else ("🔴 Skipped" if a.review_status == ReviewStatus.rejected else "🟡 Pending")

        rows += (
            f"<tr>"
            f"<td><b>{a.score}</b></td>"
            f"<td>{cat_badge}{a.position}{warning_badge}</td>"
            f"<td>{a.company}</td>"
            f"<td>{loc}</td>"
            f"<td>{source_display}</td>"
            f"<td>{date_str}</td>"
            f"<td>{rev_badge}</td>"
            f"<td>{url_link}</td>"
            f"<td style='font-size:.75rem;color:#555'>{reason_short}{query_info}</td>"
            f"<td>"
            f"<button onclick='setReview({a.id}, \"approved\")' style='font-size:0.75rem;padding:2px 6px;margin-right:3px;cursor:pointer;'>Mark Reviewed</button>"
            f"<button onclick='setReview({a.id}, \"rejected\")' style='font-size:0.75rem;padding:2px 6px;cursor:pointer;'>Skip</button>"
            f"</td>"
            f"</tr>"
        )
    if not rows:
        rows = "<tr><td colspan='10' style='text-align:center;color:#888'>No new discoveries above score threshold</td></tr>"

    sched_status = "🟢 ENABLED" if settings.discovery_scheduler_enabled else "🔴 DISABLED (Manual Only)"

    return f"""<!DOCTYPE html><html lang='fr'><head><meta charset='UTF-8'>
<title>PFE — New Discoveries & Scheduler</title>
<style>body{{font-family:system-ui,sans-serif;max-width:1200px;margin:2rem auto;padding:0 1rem}}
table{{width:100%;border-collapse:collapse;margin-bottom:1.5rem}}th,td{{text-align:left;padding:.5rem .75rem;border-bottom:1px solid #e0e0e0}}
th{{background:#f5f5f5}}a{{color:#1a73e8}}h1,h2{{margin-bottom:0.5rem}}
.card{{background:#f8f9fa;border:1px solid #e0e0e0;border-radius:8px;padding:1rem;margin-bottom:1.5rem}}
</style></head>
<body><h1>🔍 New Discoveries & Scheduler Dashboard</h1>
<p><a href='/ui'>← Dashboard</a> | <a href='/docs'>API docs</a> | Found <b>{len(apps)}</b> opportunities (score ≥ {min_score})</p>

<div class='card'>
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:0.75rem;">
    <div>
      <h3 style="margin:0 0 0.25rem 0;">⏱️ Automated Discovery Scheduler</h3>
      <p style="margin:0;font-size:0.9rem;color:#555;">
        Status: <b>{sched_status}</b> |
        Interval: <b>{settings.discovery_scheduler_interval_hours}h</b> |
        Max Runtime: <b>{settings.discovery_scheduler_max_runtime_minutes}m</b>
      </p>
    </div>
    <button id='run-btn' onclick="runDiscoveryNow()" style="background:#1a73e8;color:#fff;border:none;padding:0.6rem 1.2rem;border-radius:4px;cursor:pointer;font-weight:bold;">
      ▶ Run Discovery Now
    </button>
  </div>
  <h4 style="margin:0.75rem 0 0.5rem 0;">Recent Discovery Runs</h4>
  <table><thead><tr><th>Run ID</th><th>Status</th><th>Started</th><th>Fetched</th><th>New</th><th>High-Value PFE</th><th>Duration</th></tr></thead>
  <tbody>{run_rows}</tbody></table>
</div>

<h2>Discovered Opportunities</h2>
<table><thead><tr><th>Score</th><th>Title</th><th>Company</th><th>Location</th>
<th>Source</th><th>Discovered</th><th>Review Status</th><th>Link</th><th>Match Reason</th><th>Action</th></tr></thead>
<tbody>{rows}</tbody></table>

<script>
async function setReview(id, status) {{
  await fetch('/applications/' + id + '/review', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{review_status: status}})
  }});
  location.reload();
}}
async function runDiscoveryNow() {{
  const btn = document.getElementById('run-btn');
  btn.disabled = true;
  btn.textContent = '⏳ Running Discovery...';
  try {{
    const r = await fetch('/discovery/run', {{method: 'POST'}});
    const d = await r.json();
    alert('Discovery run ' + d.run_id + ' completed! Status: ' + d.status + '\\nNew Jobs: ' + d.new_opportunities + '\\nHigh-Value PFE: ' + d.new_high_value_opportunities);
    location.reload();
  }} catch(e) {{
    alert('Failed to trigger discovery: ' + e);
  }} finally {{
    btn.disabled = false;
    btn.textContent = '▶ Run Discovery Now';
  }}
}}
</script>
</body></html>"""


@app.get("/ui/follow-ups", response_class=HTMLResponse, include_in_schema=False)
def ui_follow_ups(db: Session = Depends(get_db)) -> str:
    due = get_due_follow_ups(db)
    today = __import__("datetime").date.today()
    rows = ""
    for app_obj in due:
        if app_obj.next_follow_up:
            delta = (app_obj.next_follow_up - today).days
            if delta < 0:
                due_label = f"<span style='color:#c62828'>Overdue ({abs(delta)}d)</span>"
            elif delta == 0:
                due_label = "<span style='color:#e65100'>Today</span>"
            else:
                due_label = f"{delta} day{'s' if delta > 1 else ''}"
        else:
            due_label = "—"
        rows += (
            f"<tr>"
            f"<td>{app_obj.company}</td>"
            f"<td>{app_obj.position}</td>"
            f"<td>{due_label}</td>"
            f"<td>#{app_obj.follow_up_count}</td>"
            f"<td>{app_obj.application_status.value}</td>"
            f"<td><a href='/ui/follow-ups/{app_obj.id}'>Review →</a></td>"
            f"</tr>"
        )
    if not rows:
        rows = "<tr><td colspan='6' style='text-align:center;color:#888'>No follow-ups due</td></tr>"
    return f"""<!DOCTYPE html><html lang='fr'><head><meta charset='UTF-8'>
<title>PFE — Follow-ups</title>
<style>body{{font-family:system-ui,sans-serif;max-width:960px;margin:2rem auto;padding:0 1rem}}
table{{width:100%;border-collapse:collapse}}th,td{{text-align:left;padding:.5rem .75rem;border-bottom:1px solid #e0e0e0}}
th{{background:#f5f5f5}}a{{color:#1a73e8}}</style></head>
<body><h1>📅 Follow-up Dashboard</h1>
<p><a href='/ui'>← Messages</a> | <a href='/docs'>API docs</a></p>
<table><thead><tr><th>Company</th><th>Position</th><th>Due</th><th>Follow-up #</th><th>Status</th><th>Action</th></tr></thead>
<tbody>{rows}</tbody></table></body></html>"""


@app.get("/ui/follow-ups/{application_id}", response_class=HTMLResponse, include_in_schema=False)
def ui_follow_up_review(application_id: int, db: Session = Depends(get_db)) -> str:
    app_obj = db.get(Application, application_id)
    if not app_obj:
        return HTMLResponse("<h2>Application not found</h2>", status_code=404)

    # Previous sent messages (timeline)
    sent_msgs = list(db.scalars(
        select(OutboundMessage)
        .where(OutboundMessage.application_id == application_id)
        .where(OutboundMessage.status == MessageStatus.sent)
        .order_by(OutboundMessage.sent_at)
    ))
    # Draft follow-up message (if any)
    draft = db.scalar(
        select(OutboundMessage)
        .where(OutboundMessage.application_id == application_id)
        .where(OutboundMessage.status.in_([MessageStatus.draft, MessageStatus.approved]))
        .order_by(OutboundMessage.created_at.desc())
    )
    # Timeline events
    events = list(db.scalars(
        select(__import__("app.models", fromlist=["ApplicationEvent"]).ApplicationEvent)
        .where(__import__("app.models", fromlist=["ApplicationEvent"]).ApplicationEvent.application_id == application_id)
        .order_by(__import__("app.models", fromlist=["ApplicationEvent"]).ApplicationEvent.created_at)
    ))

    hist_html = "".join(
        f"<li><code>{e.created_at.strftime('%Y-%m-%d %H:%M') if e.created_at else '?'}</code> — {e.event_type}"
        f"{': ' + e.notes if e.notes else ''}</li>"
        for e in events
    )
    sent_html = "".join(
        f"<li><b>{m.sent_at.strftime('%Y-%m-%d') if m.sent_at else '?'}</b>: {(m.subject or 'No subject')[:80]}</li>"
        for m in sent_msgs
    )

    draft_section = ""
    if draft:
        subj_input = (
            f"<div class='field'><label>Subject</label>"
            f"<input id='subject' type='text' value=\"{(draft.subject or '').replace(chr(34), '&quot;')}\" style='width:100%'></div>"
            if draft.channel.value == "email" else ""
        )
        draft_section = f"""
<h3>Draft Follow-up (#{draft.id})</h3>
<div class='card'>
{subj_input}
<div class='field'><label>Body</label>
<textarea id='body' style='width:100%;height:200px;box-sizing:border-box;font-family:inherit'>{draft.body.replace('<','&lt;').replace('>','&gt;')}</textarea></div>
<button class='btn btn-neutral' onclick='saveEdits()'>💾 Save</button>
{'<button class="btn btn-success" onclick="doApprove()">✅ Approve</button>' if draft.status.value == "draft" else ''}
{'<button class="btn btn-primary" onclick="doSend()">📤 Send</button>' if draft.status.value == "approved" else ''}
</div>
<script>
const mid={draft.id};
async function api(p,m,b){{const r=await fetch(p,{{method:m,headers:{{'Content-Type':'application/json'}},body:b?JSON.stringify(b):undefined}});
const d=await r.json();document.getElementById('msg').textContent=(r.ok?'Done: '+d.status:d.detail||JSON.stringify(d));setTimeout(()=>location.reload(),1500);}}
function saveEdits(){{const p={{body:document.getElementById('body').value}};const s=document.getElementById('subject');if(s)p.subject=s.value;api('/messages/'+mid,'PATCH',p);}}
function doApprove(){{api('/messages/'+mid+'/approve','POST');}}
function doSend(){{const to=prompt('Recipient email:');api('/messages/'+mid+'/send','POST',to?{{to_email:to}}:{{}});}}
</script>"""
    else:
        draft_section = f"""<p>No draft follow-up yet.
<button class='btn btn-primary' onclick="generateFollowUp()">⚡ Generate Follow-up</button></p>
<script>
async function generateFollowUp(){{
  const r=await fetch('/applications/{application_id}/messages/generate',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{channel:'email',provider:'template'}})}});
  const d=await r.json();document.getElementById('msg').textContent=r.ok?'Generated message #'+d.id:'Error: '+d.detail;setTimeout(()=>location.reload(),1500);
}}
</script>"""

    return f"""<!DOCTYPE html><html lang='fr'><head><meta charset='UTF-8'><title>Follow-up Review</title>
<style>body{{font-family:system-ui,sans-serif;max-width:820px;margin:2rem auto;padding:0 1rem}}
.card{{background:#fff;border:1px solid #ddd;border-radius:8px;padding:1.25rem;margin-bottom:1rem}}
.field{{margin-bottom:.75rem}}label{{font-weight:600;display:block;margin-bottom:.25rem;font-size:.875rem}}
.btn{{padding:.45rem 1rem;border:none;border-radius:4px;cursor:pointer;font-size:.9rem;margin-right:.4rem}}
.btn-primary{{background:#1a73e8;color:#fff}}.btn-success{{background:#1a8c1a;color:#fff}}
.btn-neutral{{background:#666;color:#fff}}</style></head><body>
<p><a href='/ui/follow-ups'>← Back</a></p>
<h2>Follow-up Review: {app_obj.company} — {app_obj.position}</h2>
<div class='card'>
<b>Status:</b> {app_obj.application_status.value} |
<b>Follow-ups sent:</b> {app_obj.follow_up_count} |
<b>Next due:</b> {app_obj.next_follow_up or 'not scheduled'} |
<b>Last contact:</b> {app_obj.last_contact or '—'}
</div>
<h3>Previous Messages</h3><ul>{sent_html or '<li>None</li>'}</ul>
<h3>Timeline</h3><ul>{hist_html or '<li>Empty</li>'}</ul>
{draft_section}
<div id='msg' style='margin-top:1rem;color:#555'></div>
</body></html>"""


@app.get("/ui/messages/{message_id}", response_class=HTMLResponse, include_in_schema=False)
def ui_review_message(message_id: int, db: Session = Depends(get_db)) -> str:
    message = db.get(OutboundMessage, message_id)
    if not message:
        return HTMLResponse("<h2>Message not found</h2>", status_code=404)
    application = db.get(Application, message.application_id)
    contact = db.get(Contact, message.contact_id) if message.contact_id else None

    subject_field = ""
    if message.channel.value == "email":
        subject_field = f"""
        <div class='field'><label>Subject</label>
        <input id='subject' type='text' value="{(message.subject or '').replace('"', '&quot;')}" style='width:100%'>
        </div>"""

    linkedin_note = ""
    if message.channel.value == "linkedin":
        profile_url = contact.linkedin_url if contact else ""
        linkedin_note = (
            f"<div class='info'>💡 LinkedIn sending is not automated. "
            f"Copy the message below and send it manually. "
            + (f"<a href='{profile_url}' target='_blank'>Open LinkedIn profile →</a>" if profile_url else "")
            + "</div>"
        )

    status_colors = {"draft": "#f0a500", "approved": "#1a8c1a", "sent": "#1a73e8", "rejected": "#c62828", "failed": "#c62828"}
    status_color = status_colors.get(message.status.value, "#666")

    can_edit = message.status.value in ("draft", "approved")
    can_approve = message.status.value == "draft"
    can_send = message.status.value == "approved" and message.channel.value == "email"

    return f"""<!DOCTYPE html><html lang='fr'><head><meta charset='UTF-8'>
<title>Review Message #{message.id}</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:800px;margin:2rem auto;padding:0 1rem}}
.card{{background:#fff;border:1px solid #ddd;border-radius:8px;padding:1.25rem;margin-bottom:1rem}}
.field{{margin-bottom:.75rem}}label{{display:block;font-weight:600;margin-bottom:.25rem;font-size:.875rem;color:#444}}
input,textarea{{width:100%;box-sizing:border-box;border:1px solid #ccc;border-radius:4px;padding:.5rem;font-family:inherit;font-size:.95rem}}
textarea{{height:260px;resize:vertical}}.btn{{display:inline-block;padding:.5rem 1.25rem;border:none;border-radius:5px;cursor:pointer;font-size:.95rem;margin-right:.5rem;margin-top:.5rem}}
.btn-primary{{background:#1a73e8;color:#fff}}.btn-success{{background:#1a8c1a;color:#fff}}
.btn-danger{{background:#c62828;color:#fff}}.btn-neutral{{background:#666;color:#fff}}
.status{{display:inline-block;padding:.2rem .75rem;border-radius:12px;color:#fff;font-size:.85rem;font-weight:600;background:{status_color}}}
.info{{background:#e8f4fd;border:1px solid #90caf9;border-radius:6px;padding:.75rem;margin-bottom:1rem;font-size:.9rem}}
#msg{{color:#555;margin-top:.5rem;font-size:.9rem}}
</style></head><body>
<p><a href='/ui'>← Back to review list</a></p>
<h2>Message #{message.id} &nbsp; <span class='status'>{message.status.value.upper()}</span></h2>

<div class='card'>
  <div class='field'><label>Company</label><span>{application.company if application else '?'}</span></div>
  <div class='field'><label>Position</label><span>{application.position if application else '?'}</span></div>
  <div class='field'><label>Channel</label><span>{message.channel.value}</span></div>
  {f"<div class='field'><label>Contact</label><span>{contact.name} – {contact.job_title or ''} – {contact.professional_email or ''}</span></div>" if contact else ''}
  <div class='field'><label>Generated by</label><span>{message.generation_provider or 'unknown'}{(' / ' + message.generation_model) if message.generation_model else ''}</span></div>
</div>

{linkedin_note}

<div class='card'>
{subject_field}
  <div class='field'><label>Body</label>
  <textarea id='body'>{message.body.replace('<', '&lt;').replace('>', '&gt;')}</textarea>
  </div>
  {'<button class="btn btn-neutral" onclick="saveEdits()">💾 Save edits</button>' if can_edit else ''}
</div>

<div>
  {'<button class="btn btn-success" onclick="doApprove()">✅ Approve</button>' if can_approve else ''}
  {'<button class="btn btn-primary" onclick="doSend()">📤 Send email</button>' if can_send else ''}
  {'<button class="btn btn-danger" onclick="doReject()">✗ Reject</button>' if message.status.value in ("draft","approved") else ''}
  <div id='msg'></div>
</div>

<script>
const mid = {message.id};
const msg = document.getElementById('msg');
const channel = '{message.channel.value}';

async function api(path, method, body) {{
  const r = await fetch(path, {{method, headers:{{'Content-Type':'application/json'}}, body: body ? JSON.stringify(body) : undefined}});
  const data = await r.json();
  if (!r.ok) {{ msg.style.color='#c62828'; msg.textContent = data.detail || JSON.stringify(data); }}
  else {{ msg.style.color='#1a8c1a'; msg.textContent = 'Done! Status: ' + data.status; setTimeout(()=>location.reload(), 1200); }}
}}

function saveEdits() {{
  const patch = {{body: document.getElementById('body').value}};
  const subjEl = document.getElementById('subject');
  if (subjEl) patch.subject = subjEl.value;
  api(`/messages/${{mid}}`, 'PATCH', patch);
}}
function doApprove() {{ api(`/messages/${{mid}}/approve`, 'POST'); }}
function doReject() {{ if(confirm('Reject this message?')) api(`/messages/${{mid}}/reject`, 'POST'); }}
function doSend() {{
  const to = prompt('Recipient email address (leave blank to use discovered email):');
  api(`/messages/${{mid}}/send`, 'POST', to ? {{to_email: to}} : {{}});
}}
</script>
</body></html>"""


@app.get("/ui/applications/{application_id}/apply", response_class=HTMLResponse, include_in_schema=False)
def ui_application_review(application_id: int, db: Session = Depends(get_db)) -> str:
    app_obj = db.get(Application, application_id)
    if not app_obj:
        return HTMLResponse("<h2>Application not found</h2>", status_code=404)
        
    payload = app_obj.application_payload
    payload_html = ""
    buttons_html = ""
    
    if payload:
        missing_html = ""
        if payload.get("missing_fields"):
            missing_html = "<ul>" + "".join(f"<li style='color:red'>{f}</li>" for f in payload["missing_fields"]) + "</ul>"
            
        q_html = ""
        for q in payload.get("questions", []):
            answer = payload.get("answers", {}).get(q["id"], "")
            q_html += f"<div class='field'><label>{q['label']} ({q['type']}) {'*' if q.get('required') else ''}</label>"
            q_html += f"<input id='q_{q['id']}' type='text' value=\"{str(answer).replace(chr(34), '&quot;')}\" style='width:100%'></div>"
            
        cand = payload.get("candidate", {})
        cand_html = f"<ul><li>Name: {cand.get('first_name')} {cand.get('last_name')}</li><li>Email: {cand.get('email')}</li><li>CV: {cand.get('cv_path')}</li></ul>"
            
        payload_html = f"""
        <h3>Payload Preparation</h3>
        <p><b>Provider:</b> {payload.get('provider_name')} | <b>Ready:</b> {'Yes' if payload.get('is_ready') else 'No'}</p>
        {f"<h4>Missing Fields</h4>{missing_html}" if missing_html else ""}
        <h4>Candidate Info</h4>
        {cand_html}
        <h4>Questions & Answers</h4>
        <div id='answers-form'>{q_html}</div>
        """
        
        is_notion_internal = "notion.so" in (app_obj.job_url or "").lower()
        if payload.get('provider_name') == 'Manual':
            if is_notion_internal:
                buttons_html = f"""
                <p style='color:#721c24;background:#f8d7da;padding:0.5rem 0.75rem;border-radius:4px;margin-bottom:0.75rem;'>⚠️ No external job listing URL available for this opportunity.</p>
                <button class='btn btn-success' onclick='markApplied()'>✅ J'ai envoyé / Mark as Applied</button>
                """
            else:
                buttons_html = f"""
                <a href='{app_obj.job_url}' target='_blank' class='btn btn-primary'>🌐 Open & Apply Manually</a>
                <button class='btn btn-success' onclick='markApplied()'>✅ J'ai envoyé / Mark as Applied</button>
                """
        else:
            buttons_html = f"""
            <button class='btn btn-success' onclick='doSubmit()'>🚀 Review & Submit Application</button>
            """
    else:
        payload_html = "<p>Payload not prepared yet.</p>"
        buttons_html = f"<button class='btn btn-primary' onclick='doPrepare()'>⚙️ Prepare Application</button>"
        
    is_notion_internal = "notion.so" in (app_obj.job_url or "").lower()
    url_display = "<span style='color:#888'>None (Notion Internal Record)</span>" if is_notion_internal else f"<a href='{app_obj.job_url}' target='_blank'>{app_obj.job_url}</a>"

    return f"""<!DOCTYPE html><html lang='fr'><head><meta charset='UTF-8'><title>Application Review</title>
<style>body{{font-family:system-ui,sans-serif;max-width:820px;margin:2rem auto;padding:0 1rem}}
.card{{background:#fff;border:1px solid #ddd;border-radius:8px;padding:1.25rem;margin-bottom:1rem}}
.field{{margin-bottom:.75rem}}label{{font-weight:600;display:block;margin-bottom:.25rem;font-size:.875rem}}
.btn{{padding:.45rem 1rem;border:none;border-radius:4px;cursor:pointer;font-size:.9rem;margin-right:.4rem;text-decoration:none;display:inline-block}}
.btn-primary{{background:#1a73e8;color:#fff}}.btn-success{{background:#1a8c1a;color:#fff}}
.btn-neutral{{background:#666;color:#fff}}.btn-danger{{background:#d32f2f;color:#fff}}</style></head><body>
<p><a href='/ui'>← Back</a></p>
<h2>Apply: {app_obj.company} — {app_obj.position}</h2>
<div class='card'><b>Status:</b> {app_obj.application_status.value} | <b>URL:</b> {url_display}</div>
<div class='card'>
{payload_html}
<div style='margin-top:1rem'>{buttons_html}</div>
</div>
<div id='msg' style='margin-top:1rem;color:#555'></div>
<script>
const appId={app_obj.id};
async function api(p,m,b){{
  const r=await fetch(p,{{method:m,headers:{{'Content-Type':'application/json'}},body:b?JSON.stringify(b):undefined}});
  const d=await r.json();document.getElementById('msg').textContent=(r.ok?'Success':d.detail||JSON.stringify(d));
  if(r.ok) setTimeout(()=>location.reload(),1500);
}}
function doPrepare(){{api('/applications/'+appId+'/prepare','POST');}}
function doSubmit(){{
  const overrides = {{}};
  document.querySelectorAll('#answers-form input').forEach(el => {{
     overrides[el.id.replace('q_', '')] = el.value;
  }});
  api('/applications/'+appId+'/submit','POST', {{answers_override: overrides}});
}}
function markApplied(){{api('/applications/'+appId+'/status','POST', {{status: 'applied', notes: 'Manual application'}});}}
</script></body></html>"""


# ===========================================================================
# Notion CRM Integration — Phase 1: Read-only import (Notion -> SQLite)
# ===========================================================================

@app.get("/notion/status", response_model=NotionConnectionStatus, tags=["notion"])
def notion_status() -> NotionConnectionStatus:
    """Return Notion integration connection status.

    Never exposes the API key or full database ID.
    """
    settings = get_settings()
    configured = bool(settings.notion_api_key and settings.notion_database_id)
    db_suffix: str | None = None
    if configured and settings.notion_database_id:
        raw = settings.notion_database_id.replace("-", "")
        db_suffix = f"...{raw[-8:]}" if len(raw) >= 8 else "***"
    return NotionConnectionStatus(
        configured=configured,
        database_id_suffix=db_suffix,
        error=None if configured else "NOTION_API_KEY and/or NOTION_DATABASE_ID not set in .env",
    )


@app.get("/notion/preview", response_model=NotionSyncResult, tags=["notion"])
def notion_preview(db: Session = Depends(get_db)) -> NotionSyncResult:
    """Fetch and preview all Notion pages without writing anything.

    Always runs in dry_run=True mode.
    """
    settings = get_settings()
    result = import_from_notion(db=db, settings=settings, dry_run=True, preview_limit=200)
    return NotionSyncResult(
        dry_run=True,
        pages_read=result.pages_read,
        new_imported=result.new_imported,
        matched_existing=result.matched_existing,
        skipped_empty=result.skipped_empty,
        errors=result.errors,
        preview=result.preview,
    )


@app.post("/notion/import", response_model=NotionSyncResult, tags=["notion"])
def notion_import(
    dry_run: bool = True,
    db: Session = Depends(get_db),
) -> NotionSyncResult:
    """Import Notion CRM records into SQLite.

    - dry_run=true (default): Preview only — ZERO SQLite or Notion writes.
    - dry_run=false: Persist new records and update notion_page_id on matches.

    The Notion database is NEVER modified by this endpoint.
    """
    settings = get_settings()
    result = import_from_notion(db=db, settings=settings, dry_run=dry_run, preview_limit=200)
    return NotionSyncResult(
        dry_run=result.dry_run,
        pages_read=result.pages_read,
        new_imported=result.new_imported,
        matched_existing=result.matched_existing,
        skipped_empty=result.skipped_empty,
        errors=result.errors,
        preview=result.preview,
    )


@app.get("/ui/notion", response_class=HTMLResponse, tags=["ui"])
def ui_notion(db: Session = Depends(get_db)) -> str:
    """Notion CRM integration dashboard."""
    settings = get_settings()
    configured = bool(settings.notion_api_key and settings.notion_database_id)
    db_suffix = ""
    if configured and settings.notion_database_id:
        raw = settings.notion_database_id.replace("-", "")
        db_suffix = f"...{raw[-8:]}" if len(raw) >= 8 else "***"

    status_badge = (
        "<span style='background:#1a8c1a;color:#fff;padding:.2rem .7rem;border-radius:4px'>✓ Connected</span>"
        if configured else
        "<span style='background:#d32f2f;color:#fff;padding:.2rem .7rem;border-radius:4px'>✗ Not configured</span>"
    )

    config_hint = "" if configured else """
    <div class='card' style='border-color:#d32f2f'>
      <b>Setup required</b><br>
      Add the following to your <code>.env</code> file:<br>
      <pre>NOTION_API_KEY=secret_...
NOTION_DATABASE_ID=your-database-id</pre>
      Never share or commit these values.
    </div>"""

    return f"""<!DOCTYPE html><html lang='fr'><head><meta charset='UTF-8'>
<title>Notion CRM — PFE Automation</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:960px;margin:2rem auto;padding:0 1rem}}
h1{{font-size:1.5rem;margin-bottom:1rem}}
.card{{background:#fff;border:1px solid #ddd;border-radius:8px;padding:1.25rem;margin-bottom:1rem}}
.btn{{padding:.45rem 1rem;border:none;border-radius:4px;cursor:pointer;font-size:.9rem;margin-right:.4rem}}
.btn-primary{{background:#1a73e8;color:#fff}}.btn-success{{background:#1a8c1a;color:#fff}}
.btn-neutral{{background:#666;color:#fff}}.btn-danger{{background:#d32f2f;color:#fff}}
table{{width:100%;border-collapse:collapse;font-size:.85rem}}
th,td{{text-align:left;padding:.4rem .6rem;border-bottom:1px solid #eee}}
th{{background:#f5f5f5;font-weight:600}}
.badge-new{{background:#1a73e8;color:#fff;border-radius:3px;padding:.1rem .4rem;font-size:.75rem}}
.badge-match{{background:#f09300;color:#fff;border-radius:3px;padding:.1rem .4rem;font-size:.75rem}}
.badge-skip{{background:#888;color:#fff;border-radius:3px;padding:.1rem .4rem;font-size:.75rem}}
#result{{display:none}}
</style></head><body>
<p><a href='/ui'>← Dashboard</a></p>
<h1>📒 Notion CRM Integration</h1>

<div class='card'>
  <b>Status:</b> {status_badge}
  {"<br><b>Database:</b> " + db_suffix if db_suffix else ""}
  <br><small style='color:#888'>Phase 1: Notion → SQLite import only. Notion records are never modified.</small>
</div>
{config_hint}

<div class='card'>
  <h3 style='margin:0 0 .75rem'>Actions</h3>
  <button class='btn btn-neutral' onclick='runPreview()' {'disabled' if not configured else ''}>🔍 Preview (read-only)</button>
  <button class='btn btn-primary' onclick='runImport(true)' {'disabled' if not configured else ''}>📥 Dry-Run Import</button>
  <button class='btn btn-success' onclick='confirmImport()' {'disabled' if not configured else ''} id='confirmBtn' style='display:none'>✅ Confirm Import (write)</button>
  <p style='margin:.75rem 0 0;font-size:.85rem;color:#555'>
    <b>Preview</b> fetches Notion pages and shows what would change — no writes.<br>
    <b>Dry-Run Import</b> simulates the full import — no SQLite or Notion writes.<br>
    <b>Confirm Import</b> appears after a dry-run and persists changes to SQLite only.
  </p>
</div>

<div id='result'>
  <div class='card'>
    <b>Result</b> <span id='dry-badge'></span><br>
    Pages read: <b id='pages'>0</b> |
    New: <b id='new'>0</b> |
    Matched: <b id='matched'>0</b> |
    Skipped: <b id='skipped'>0</b> |
    Errors: <b id='errs'>0</b>
  </div>
  <div id='err-box' class='card' style='display:none;border-color:#d32f2f'>
    <b>Errors:</b><pre id='err-list' style='color:#d32f2f;font-size:.8rem'></pre>
  </div>
  <div class='card'>
    <b>Preview</b> (first 200 records)
    <table id='preview-table'>
      <thead><tr><th>Action</th><th>Company</th><th>Title</th><th>Job URL</th><th>Notion Status</th><th>Mapped Status</th><th>Tags</th></tr></thead>
      <tbody id='preview-body'></tbody>
    </table>
  </div>
</div>

<script>
let lastWasDryRun = true;
async function callApi(path) {{
  document.getElementById('result').style.display='none';
  try {{
    const r = await fetch(path, {{method: path.includes('import') ? 'POST' : 'GET'}});
    const d = await r.json();
    renderResult(d);
    return d;
  }} catch(e) {{
    alert('API error: ' + e);
  }}
}}
function renderResult(d) {{
  document.getElementById('result').style.display='';
  document.getElementById('dry-badge').innerHTML = d.dry_run
    ? "<span style='background:#f09300;color:#fff;border-radius:3px;padding:.1rem .4rem;font-size:.8rem'>DRY RUN</span>"
    : "<span style='background:#1a8c1a;color:#fff;border-radius:3px;padding:.1rem .4rem;font-size:.8rem'>LIVE</span>";
  document.getElementById('pages').textContent = d.pages_read;
  document.getElementById('new').textContent = d.new_imported;
  document.getElementById('matched').textContent = d.matched_existing;
  document.getElementById('skipped').textContent = d.skipped_empty;
  document.getElementById('errs').textContent = d.errors.length;
  if (d.errors.length > 0) {{
    document.getElementById('err-box').style.display='';
    document.getElementById('err-list').textContent = d.errors.join('\\n');
  }} else {{
    document.getElementById('err-box').style.display='none';
  }}
  const tbody = document.getElementById('preview-body');
  tbody.innerHTML = '';
  (d.preview || []).forEach(row => {{
    const cls = row.action === 'NEW' ? 'badge-new' : row.action === 'MATCH' ? 'badge-match' : 'badge-skip';
    const tags = (row.tags || []).join(', ');
    const url = row.job_url ? `<a href="${{row.job_url}}" target="_blank">link</a>` : '—';
    tbody.innerHTML += `<tr>
      <td><span class="${{cls}}">${{row.action}}</span></td>
      <td>${{row.company || '—'}}</td>
      <td>${{row.title || '—'}}</td>
      <td>${{url}}</td>
      <td>${{row.notion_statut || '—'}}</td>
      <td>${{row.mapped_status || '—'}}</td>
      <td style='font-size:.75rem'>${{tags}}</td>
    </tr>`;
  }});
  document.getElementById('confirmBtn').style.display = d.dry_run ? '' : 'none';
}}
async function runPreview() {{ await callApi('/notion/preview'); lastWasDryRun=true; }}
async function runImport(isDry) {{ await callApi('/notion/import?dry_run=' + isDry); lastWasDryRun=isDry; }}
function confirmImport() {{ if(confirm('This will write to SQLite. Notion is NOT modified. Proceed?')) runImport(false); }}
</script></body></html>"""

