"""Blaze speed-to-lead pipeline.

Receives a Facebook lead-ad webhook, then (in the background) logs the lead to a
Google Sheet, scores it with Claude, alerts Slack, emails a drafted reply, and
records how many seconds the whole thing took.

Goal: cut lead response time from ~22 hours to under 60 seconds.
"""

import logging
import os
import re
import smtplib
import ssl
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

import gspread
from gspread.utils import rowcol_to_a1
import requests
from anthropic import Anthropic
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI
from google.oauth2.service_account import Credentials
from pydantic import BaseModel, EmailStr, Field

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("blaze")

# --- Config (loaded once) ---------------------------------------------------

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-5-5")
CLAUDE_MODEL_FALLBACK = "claude-haiku-4-5"

GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
GOOGLE_CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "google-credentials.json")

SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")

GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
LEAD_INBOX_EMAIL = os.getenv("LEAD_INBOX_EMAIL")

BOOKING_LINK = os.getenv("BOOKING_LINK") or "[booking link]"

PROMPT_PATH = Path(__file__).parent / "prompts" / "qualify_lead.md"

SHEET_COLUMNS = [
    "received_at",
    "name",
    "email",
    "phone",
    "company",
    "num_doors",
    "message",
    "fit_score",
    "fit_reason",
    "reply_subject",
    "reply_body",
    "slack_sent_at",
    "email_sent_at",
    "seconds_to_rep_alert",
    "seconds_to_lead_reply",
]

# Column letters derived from the schema so ranges stay correct if it changes.
LAST_COL = re.sub(r"\d+$", "", rowcol_to_a1(1, len(SHEET_COLUMNS)))
AI_START_COL = re.sub(
    r"\d+$", "", rowcol_to_a1(1, SHEET_COLUMNS.index("fit_score") + 1)
)

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# JSON schema for Claude's structured output (tool use).
LEAD_ASSESSMENT_TOOL = {
    "name": "submit_lead_assessment",
    "description": "Submit the fit score and drafted reply for an inbound lead.",
    "input_schema": {
        "type": "object",
        "properties": {
            "fit_score": {
                "type": "integer",
                "minimum": 1,
                "maximum": 5,
                "description": "Fit for Blaze, 1 (not a fit) to 5 (perfect fit).",
            },
            "fit_reason": {
                "type": "string",
                "description": "One-line explanation of the score.",
            },
            "reply_subject": {
                "type": "string",
                "description": "Subject line for the reply email.",
            },
            "reply_body": {
                "type": "string",
                "description": "Short, human-sounding reply body.",
            },
        },
        "required": ["fit_score", "fit_reason", "reply_subject", "reply_body"],
    },
}


# --- Models -----------------------------------------------------------------


class Lead(BaseModel):
    full_name: str = Field(..., min_length=1)
    email: EmailStr
    phone: str = Field(..., min_length=1)
    company: str = Field(..., min_length=1)
    num_doors: int = Field(..., ge=0)
    message: str = ""


# --- Helpers ----------------------------------------------------------------


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_worksheet():
    """Open the first worksheet of the configured spreadsheet."""
    creds = Credentials.from_service_account_file(
        GOOGLE_CREDENTIALS_FILE, scopes=GOOGLE_SCOPES
    )
    client = gspread.authorize(creds)
    sheet = client.open_by_key(GOOGLE_SHEET_ID)
    return sheet.sheet1


def ensure_header(ws) -> None:
    """Guarantee the header is row 1 (bold + frozen), with clip wrapping so long
    replies don't blow up row height."""
    values = ws.get_all_values()
    if values and values[0] == SHEET_COLUMNS:
        return
    if not values:
        ws.update(range_name="A1", values=[SHEET_COLUMNS], value_input_option="RAW")
    else:
        # Data already sitting in row 1 (no header) — push it down and insert.
        ws.insert_row(SHEET_COLUMNS, index=1, value_input_option="RAW")
    ws.freeze(rows=1)
    ws.format(f"A1:{LAST_COL}1", {"textFormat": {"bold": True}})
    ws.format(f"A:{LAST_COL}", {"wrapStrategy": "CLIP"})


def append_lead(ws, lead: Lead, received_at: str) -> int:
    """Ensure the header exists, append the lead, return the row number."""
    ensure_header(ws)
    row = [
        received_at,
        lead.full_name,
        lead.email,
        lead.phone,
        lead.company,
        lead.num_doors,
        lead.message,
    ]
    ws.append_row(row, value_input_option="RAW")
    return len(ws.get_all_values())


def load_prompt(lead: Lead) -> str:
    # Plain .replace (not str.format) so the {{BOOKING_LINK}} placeholder in the
    # prompt survives untouched and reaches Claude intact.
    template = PROMPT_PATH.read_text(encoding="utf-8")
    fields = {
        "{full_name}": lead.full_name,
        "{email}": lead.email,
        "{phone}": lead.phone,
        "{company}": lead.company,
        "{num_doors}": str(lead.num_doors),
        "{message}": lead.message or "(no message)",
    }
    for token, value in fields.items():
        template = template.replace(token, value)
    return template


def score_lead(lead: Lead) -> dict:
    """Score the lead with Claude via tool use. Falls back to haiku-4-5 if the
    configured model is not found. Raises on any other failure."""
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = load_prompt(lead)

    def call(model: str):
        return client.messages.create(
            model=model,
            max_tokens=1024,
            tools=[LEAD_ASSESSMENT_TOOL],
            tool_choice={"type": "tool", "name": "submit_lead_assessment"},
            messages=[{"role": "user", "content": prompt}],
        )

    try:
        resp = call(CLAUDE_MODEL)
    except Exception as exc:
        if "not_found" in str(exc).lower() or "not found" in str(exc).lower():
            log.warning(
                "Model %s not found; falling back to %s",
                CLAUDE_MODEL,
                CLAUDE_MODEL_FALLBACK,
            )
            resp = call(CLAUDE_MODEL_FALLBACK)
        else:
            raise

    for block in resp.content:
        if block.type == "tool_use" and block.name == "submit_lead_assessment":
            assessment = dict(block.input)
            body = assessment.get("reply_body", "")
            # Claude may emit single or double braces; fill both.
            for token in ("{{BOOKING_LINK}}", "{BOOKING_LINK}"):
                body = body.replace(token, BOOKING_LINK)
            assessment["reply_body"] = body
            return assessment

    raise RuntimeError("Claude did not return a tool_use block")


def score_emoji(score) -> str:
    try:
        score = int(score)
    except (TypeError, ValueError):
        return "❓"
    if score >= 4:
        return "🔥"
    if score == 3:
        return "👍"
    return "🥶"


def send_slack(lead: Lead, assessment: dict | None) -> str:
    """Post a Slack alert. If assessment is None, send the raw lead so nothing is
    lost. Returns the timestamp it was sent."""
    if not SLACK_WEBHOOK_URL:
        log.warning("SLACK_WEBHOOK_URL not set; skipping Slack alert")
        return ""

    if assessment is None:
        text = (
            "⚠️ *Lead scoring failed — raw lead (follow up manually)*\n"
            f"*Name:* {lead.full_name}\n"
            f"*Company:* {lead.company}\n"
            f"*Doors:* {lead.num_doors}\n"
            f"*Phone:* {lead.phone}\n"
            f"*Email:* {lead.email}\n"
            f"*Message:* {lead.message or '(none)'}"
        )
    else:
        score = assessment.get("fit_score")
        lines = [
            f"{score_emoji(score)} *Lead score {score}/5* — {assessment.get('fit_reason', '')}",
            f"*Name:* {lead.full_name}",
            f"*Company:* {lead.company}",
            f"*Doors:* {lead.num_doors}",
            f"*Phone:* {lead.phone}",
        ]
        try:
            if int(score) >= 4:
                lines.append("📞 *Call within 5 min*")
        except (TypeError, ValueError):
            pass
        text = "\n".join(lines)

    resp = requests.post(SLACK_WEBHOOK_URL, json={"text": text}, timeout=10)
    resp.raise_for_status()
    return now_iso()


def send_email(lead: Lead, assessment: dict) -> str:
    """Email the drafted reply to LEAD_INBOX_EMAIL. Returns the timestamp sent."""
    if not (GMAIL_ADDRESS and GMAIL_APP_PASSWORD and LEAD_INBOX_EMAIL):
        log.warning("Gmail/LEAD_INBOX_EMAIL not fully configured; skipping email")
        return ""

    msg = EmailMessage()
    msg["Subject"] = assessment.get("reply_subject", "Thanks for reaching out")
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = LEAD_INBOX_EMAIL
    msg.set_content(assessment.get("reply_body", ""))

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.send_message(msg)
    return now_iso()


def update_row(ws, row: int, assessment: dict | None, slack_at: str,
               email_at: str, secs_alert, secs_reply) -> None:
    """Write AI fields + both timings back to the lead's row."""
    a = assessment or {}
    values = [[
        a.get("fit_score", ""),
        a.get("fit_reason", ""),
        a.get("reply_subject", ""),
        a.get("reply_body", ""),
        slack_at,
        email_at,
        round(secs_alert, 2) if secs_alert is not None else "",
        round(secs_reply, 2) if secs_reply is not None else "",
    ]]
    ws.update(
        range_name=f"{AI_START_COL}{row}:{LAST_COL}{row}",
        values=values,
        value_input_option="RAW",
    )


# --- Pipeline ---------------------------------------------------------------


def process_lead(lead: Lead, received_at: str, started: float) -> None:
    log.info("Processing lead: %s (%s, %s doors)", lead.full_name, lead.company,
             lead.num_doors)

    # 1. Log to the sheet immediately.
    ws = None
    row = None
    try:
        ws = get_worksheet()
        row = append_lead(ws, lead, received_at)
        log.info("Appended to sheet at row %s", row)
    except Exception:
        log.exception("Failed to append lead to sheet (continuing)")

    # 2. Score with Claude. On any failure, keep going with assessment=None so the
    #    lead still reaches Slack.
    assessment = None
    try:
        assessment = score_lead(lead)
        log.info("Scored %s/5 — %s", assessment.get("fit_score"),
                 assessment.get("fit_reason"))
    except Exception:
        log.exception("Claude scoring failed; sending raw lead to Slack")

    # 3. Fire the Slack alert and the lead reply email at the same time. The
    #    email (SMTP) is the slow leg, so we don't make Slack wait behind it.
    slack_at, email_at = "", ""
    secs_alert, secs_reply = None, None

    def do_slack():
        at = send_slack(lead, assessment)
        return at, (time.monotonic() - started if at else None)

    def do_email():
        at = send_email(lead, assessment)
        return at, (time.monotonic() - started if at else None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        slack_future = pool.submit(do_slack)
        email_future = pool.submit(do_email) if assessment is not None else None

        try:
            slack_at, secs_alert = slack_future.result()
            if slack_at:
                log.info("Slack alert sent — rep alerted in %.2fs", secs_alert)
        except Exception:
            log.exception("Failed to send Slack alert")

        if email_future is not None:
            try:
                email_at, secs_reply = email_future.result()
                if email_at:
                    log.info("Reply emailed to %s — lead replied in %.2fs",
                             LEAD_INBOX_EMAIL, secs_reply)
            except Exception:
                log.exception("Failed to send reply email")

    # 4. Update the sheet last, once everything else is done.
    if ws is not None and row is not None:
        try:
            update_row(ws, row, assessment, slack_at, email_at, secs_alert,
                       secs_reply)
        except Exception:
            log.exception("Failed to update sheet row with timings")

    total = time.monotonic() - started
    log.info(
        "Done in %.2fs (rep alert %s, lead reply %s) for %s",
        total,
        f"{secs_alert:.2f}s" if secs_alert is not None else "n/a",
        f"{secs_reply:.2f}s" if secs_reply is not None else "n/a",
        lead.full_name,
    )


# --- App --------------------------------------------------------------------

app = FastAPI(title="Blaze speed-to-lead")


@app.get("/")
def health():
    return {"status": "ok", "service": "blaze-speed-to-lead"}


@app.post("/webhook/lead", status_code=200)
def webhook_lead(lead: Lead, background_tasks: BackgroundTasks):
    received_at = now_iso()
    started = time.monotonic()
    background_tasks.add_task(process_lead, lead, received_at, started)
    return {"status": "accepted"}
