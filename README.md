# Blaze Speed-to-Lead Pipeline

Cut lead response time from ~22 hours to **under 60 seconds**.

A Facebook lead-ad webhook comes in; the API returns `200` instantly and does the
real work in the background: log to a Google Sheet, score the lead with Claude,
alert the sales team on Slack, and email a drafted reply back to the lead — then
record exactly how many seconds the whole thing took.

## What it does

1. **Webhook** — `POST /webhook/lead` accepts a fake Facebook lead-ad payload,
   validates it with Pydantic, returns `200` immediately, and runs the pipeline as
   a background task.
2. **Google Sheet** — appends the lead via `gspread` (adds the header row if the
   sheet is empty).
3. **Claude scoring** — calls the Claude API (`CLAUDE_MODEL`, default
   `claude-haiku-5-5`) with **tool use / structured output** to get a `fit_score`
   (1–5), a one-line `fit_reason`, and a drafted `reply_subject` / `reply_body`.
   The prompt lives in [`prompts/qualify_lead.md`](prompts/qualify_lead.md) and is
   loaded at runtime. If the model isn't found, it falls back to
   `claude-haiku-4-5` and logs a warning so it never breaks.
4. **Slack alert** — posts the score (with an emoji), reason, name, company, doors,
   and phone to an incoming webhook. Scores 4–5 get a **"Call within 5 min"** nudge.
5. **Email** — sends the drafted reply to `LEAD_INBOX_EMAIL` via Gmail SMTP
   (that's you, pretending to be the lead).
6. **Timing** — the Slack alert and the lead reply email are sent **concurrently**
   (SMTP is the slow leg, so the internal alert doesn't wait behind it). The sheet
   row is then updated last with the Slack/email timestamps and two timings:
   `seconds_to_rep_alert` (when Slack fired) and `seconds_to_lead_reply` (when the
   email sent). Both are logged to the console.

**No lead gets lost:** if the Claude call fails, the raw lead is still sent to
Slack for manual follow-up, and each stage is isolated so one failure doesn't
abort the rest.

## Lead fit (Blaze context)

Blaze is an AI CFO (invoicing, payments, cash flow, bookkeeping on autopilot) for
property management companies with **50–500 doors**. Tenants, single-unit
landlords, and junk score low.

## Setup

1. **Create and activate a virtualenv, install deps:**

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Add your secrets.** Copy the example and fill it in:

   ```bash
   cp .env.example .env
   ```

   | Variable | What it is |
   | --- | --- |
   | `ANTHROPIC_API_KEY` | Your Anthropic API key |
   | `CLAUDE_MODEL` | Model to use (default `claude-haiku-5-5`) |
   | `GOOGLE_SHEET_ID` | The ID from the sheet's URL (`/d/<THIS>/edit`) |
   | `GOOGLE_CREDENTIALS_FILE` | Path to the service-account JSON (`google-credentials.json`) |
   | `SLACK_WEBHOOK_URL` | Slack incoming webhook URL |
   | `GMAIL_ADDRESS` | Gmail address to send from |
   | `GMAIL_APP_PASSWORD` | Gmail **App Password** (not your login password) |
   | `LEAD_INBOX_EMAIL` | Where the drafted reply is emailed |
   | `WEBHOOK_BASE_URL` | Base URL the test script posts to |
   | `BOOKING_LINK` | (optional) booking URL dropped into replies; defaults to `[booking link]` if blank |

3. **Google service account:** create one in Google Cloud, enable the Sheets +
   Drive APIs, download the JSON as `google-credentials.json` in this folder, and
   **share the target sheet with the service-account email** (as Editor).

> `.env` and `google-credentials.json` are git-ignored and never printed.

## How to run

Start the API:

```bash
source .venv/bin/activate
uvicorn main:app --reload
```

In a second terminal, send a test lead (`strong`, `borderline`, or `junk`):

```bash
source .venv/bin/activate
python send_test_lead.py strong
```

Watch the first terminal for the pipeline log and timing.

## Sample output

Test script:

```
Posting 'strong' lead to http://127.0.0.1:8000/webhook/lead ...
HTTP 200: {"status":"accepted"}
Watch the server console for the pipeline timing.
```

Server console:

```
14:02:11  INFO     Processing lead: Dana Reyes (Summit Property Management, 210 doors)
14:02:12  INFO     Appended to sheet at row 2
14:02:14  INFO     Scored 5/5 — Property management company managing 210 doors, squarely in Blaze's 50-500 range.
14:02:14  INFO     Slack alert sent — rep alerted in 3.10s
14:02:16  INFO     Reply emailed to you@example.com — lead replied in 5.42s
14:02:16  INFO     Done in 5.45s (rep alert 3.10s, lead reply 5.42s) for Dana Reyes
```

Slack:

```
🔥 Lead score 5/5 — Property management company managing 210 doors, squarely in Blaze's 50-500 range.
Name: Dana Reyes
Company: Summit Property Management
Doors: 210
Phone: +1-512-555-0142
📞 Call within 5 min
```
