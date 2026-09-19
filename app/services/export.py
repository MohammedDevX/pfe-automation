from io import BytesIO

from openpyxl import Workbook
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Application, OutboundMessage


APP_HEADERS = [
    "application ID",
    "company",
    "position",
    "source",
    "job URL",
    "recruiter",
    "recruiter LinkedIn URL",
    "professional email",
    "discovery date",
    "posted at",
    "contact date",
    "application date",
    "application method",
    "external application ID",
    "channel",
    "application status",
    "response status",
    "response date",
    "last contact date",
    "follow-up count",
    "next follow-up",
    "next follow-up date",
    "review status",
    "score",
    "score reason",
    "notes",
]

MSG_HEADERS = [
    "message ID",
    "application ID",
    "company",
    "position",
    "channel",
    "status",
    "subject",
    "generation provider",
    "created at",
    "approved at",
    "sent at",
    "failure reason",
]


def _autofit(sheet) -> None:
    for column_cells in sheet.columns:
        length = max(len(str(cell.value or "")) for cell in column_cells)
        sheet.column_dimensions[column_cells[0].column_letter].width = min(length + 2, 60)


def applications_xlsx(db: Session) -> BytesIO:
    workbook = Workbook()

    # --- Applications sheet ---
    app_sheet = workbook.active
    app_sheet.title = "Applications"
    app_sheet.append(APP_HEADERS)

    for app in db.scalars(select(Application).order_by(Application.created_at.desc())):
        app_sheet.append(
            [
                app.id,
                app.company,
                app.position,
                app.source,
                app.job_url,
                app.recruiter,
                app.recruiter_linkedin_url,
                app.professional_email,
                app.discovery_date.isoformat() if app.discovery_date else None,
                app.posted_at.isoformat() if app.posted_at else None,
                app.contact_date.isoformat() if app.contact_date else None,
                app.application_date.isoformat() if app.application_date else None,
                app.application_method,
                app.external_application_id,
                app.channel,
                app.application_status.value,
                app.response_status.value if app.response_status else None,
                app.last_response_date.isoformat() if app.last_response_date else None,
                app.last_contact.isoformat() if app.last_contact else None,
                app.follow_up_count,
                app.next_follow_up.isoformat() if app.next_follow_up else None,
                app.review_status.value,
                app.score,
                app.score_reason,
                app.notes,
            ]
        )
    _autofit(app_sheet)

    # --- Messages sheet ---
    msg_sheet = workbook.create_sheet("Messages")
    msg_sheet.append(MSG_HEADERS)

    for msg in db.scalars(select(OutboundMessage).order_by(OutboundMessage.created_at.desc())):
        app_obj = db.get(Application, msg.application_id)
        msg_sheet.append(
            [
                msg.id,
                msg.application_id,
                app_obj.company if app_obj else "",
                app_obj.position if app_obj else "",
                msg.channel.value,
                msg.status.value,
                msg.subject,
                msg.generation_provider,
                msg.created_at.isoformat() if msg.created_at else None,
                msg.approved_at.isoformat() if msg.approved_at else None,
                msg.sent_at.isoformat() if msg.sent_at else None,
                msg.failure_reason,
            ]
        )
    _autofit(msg_sheet)

    output = BytesIO()
    workbook.save(output)
    output.seek(0)
    return output
