# AI Gmail Cleanup Agent

A Streamlit application that scans a Gmail inbox, classifies mail with a
rule engine + Gemini, and lets the user review and approve a cleanup
(archive/trash/label) before anything is changed. Each user connects their
own Gmail account via Composio OAuth - no shared credentials, no
pre-existing connections.

## Project Overview

The agent never deletes anything without explicit human confirmation. It
starts in **Dry Run** mode by default: it will scan, classify, and show
recommendations, but perform zero Gmail mutations until the operator turns
Dry Run off and explicitly confirms.

## Architecture

```
gmail-cleanup-agent/
  app.py                   Streamlit UI - orchestrates the workflow
  src/
    config.py               Centralized config: categories, thresholds, age rules
    models.py                Dataclasses: EmailMetadata, ClassificationResult, ...
    logger.py                App logging + CSV audit logger
    composio_service.py      Composio SDK wrapper (auth, tool execution, retries)
    gmail_service.py         Gmail operations built on composio_service
    rule_engine.py           Deterministic sender/subject classification rules
    classifier.py            Gemini-based classification, batched + cached
    safety_rules.py          The only module allowed to recommend TRASH
    cleanup_engine.py        Orchestrates scan -> classify -> decide -> execute
  tests/                    pytest suite
  data/cleanup_logs/        Per-user CSV audit logs (gitignored)
  data/cache/                Classification cache (gitignored)
```

Data flow: `app.py` -> `cleanup_engine.scan_and_classify` -> `gmail_service`
(fetch) -> `rule_engine` (cheap classification) -> `classifier` (Gemini, only
for what rules couldn't resolve) -> `safety_rules.decide_action` (protection +
thresholds + age rules) -> dashboard -> `cleanup_engine.execute_cleanup` ->
`gmail_service` (mutate) + `logger.AuditLogger` (record).

## Features

- Per-user Gmail OAuth via Composio (`connected_accounts.link`) - no shared
  credentials.
- 14-category classification with confidence scores.
- Deterministic rule engine resolves obvious mail before any Gemini call.
- Gemini only classifies what rules can't; results are cached by
  sender+subject hash to avoid repeat spend.
- Hard-coded protection list (finance, invoices, legal, HR, client mail,
  security/OTP within 72h, low confidence, `Uncertain`) that no
  configuration can override into auto-trash.
- Configurable confidence thresholds and per-category age rules, all in
  `src/config.py` - nothing hard-coded inline elsewhere.
- Dashboard: connection status, inbox overview, classification breakdown,
  per-category recommendations with manual override, filterable preview,
  confirmation counts, and post-run results.
- Dry Run mode (default ON) - scans and classifies with zero Gmail writes.
- CSV audit log per user per day; message bodies are never stored.

## Requirements

- Python 3.10+
- A Composio account with a Gmail auth config (OAuth client via Google Cloud
  Console, registered in Composio)
- A Google Gemini API key

## Installation

```bash
git clone <this-repo-url> gmail-cleanup-agent
cd gmail-cleanup-agent
python -m venv venv
# Windows: venv\Scripts\activate   |   macOS/Linux: source venv/bin/activate
pip install -r requirements.txt
```

## Environment Configuration

```bash
cp .env.example .env
```

Fill in:

| Variable | Description |
|---|---|
| `COMPOSIO_API_KEY` | From https://app.composio.dev/settings |
| `COMPOSIO_GMAIL_AUTH_CONFIG_ID` | ID of the Gmail auth config you create in Composio (see below) |
| `COMPOSIO_CALLBACK_URL` | Optional; leave blank to use Composio's hosted callback |
| `GEMINI_API_KEY` | Gemini API key |
| `GEMINI_MODEL` | Defaults to `gemini-3.6-flash` |
| `CONFIDENCE_HIGH` / `CONFIDENCE_LOW` | Confidence bands (defaults 0.90 / 0.70) |
| `DRY_RUN_DEFAULT` | Defaults to `true` |

## Composio Setup

1. Create an account at https://composio.dev and grab an API key.
2. In the Composio dashboard, go to **Toolkits -> Gmail -> Create Auth
   Config**, and connect it to a Google OAuth client (Client ID/Secret from
   Google Cloud Console, with the Gmail API enabled and the necessary
   scopes: `gmail.readonly`, `gmail.modify`).
3. Copy the resulting auth config ID into `COMPOSIO_GMAIL_AUTH_CONFIG_ID`.
4. This one auth config is shared infrastructure - every end user still
   authenticates their *own* Gmail account against it and gets their own
   connected account ID. Nobody sees or reuses anyone else's connection.

## Gmail Authentication (end-user flow)

1. User launches the app and clicks **Connect Gmail**.
2. The app calls `composio.connected_accounts.link(...)` and shows a Google
   OAuth authorization link.
3. User opens the link, signs into *their own* Gmail account, and grants
   consent.
4. User returns to the app and clicks **I've authorized - check status**;
   the app polls `wait_for_connection` and shows the connected address once
   active.

## Gemini Configuration

Set `GEMINI_API_KEY` and optionally `GEMINI_MODEL` (default `gemini-3.6-flash`). The classifier only
sends sender, subject, snippet, attachment flag, and age - never full email
bodies - unless a future version explicitly opts a message into deeper
inspection.

## How to Launch

```bash
streamlit run app.py
```

Open the URL Streamlit prints (typically http://localhost:8501).

## Deploying to Streamlit Community Cloud

1. Push this repo to GitHub (`app.py` must stay at repo root - it already is).
2. Go to https://share.streamlit.io -> **New app** -> pick this repo/branch,
   set main file path to `app.py`.
3. Community Cloud has no `.env` file and does not inject secrets as OS
   environment variables - it only exposes them via `st.secrets`. This
   project's `src/config.py` already handles both: it reads `os.getenv(...)`
   first (local dev, `.env`), then falls back to `st.secrets` (Cloud). You
   don't need to change any code.
4. In the app's **Settings -> Secrets** on Community Cloud, paste:
   ```toml
   COMPOSIO_API_KEY = "..."
   COMPOSIO_GMAIL_AUTH_CONFIG_ID = "..."
   GEMINI_API_KEY = "..."
   GEMINI_MODEL = "gemini-3.6-flash"
   ```
   Same keys as `.env.example`; add any of the batching/safety overrides too
   if you want non-default values on Cloud.
5. Deploy. First load will show "Not connected" per visitor - each user
   authenticates their own Gmail account through Composio, same as local.

## How Dry Run Works

Dry Run is ON by default (`DRY_RUN_DEFAULT=true`, and re-toggleable per
session in the UI). While ON:

- Inbox is scanned and classified normally.
- Recommendations, previews, and confirmation counts render normally.
- Clicking **Execute Cleanup** logs every decision to the audit CSV as
  `DRY_RUN_<ACTION>` but makes **zero** Gmail API mutation calls.

Turn Dry Run off in the Scan section to allow real changes on the next
Execute Cleanup.

## How Cleanup Works

1. Scan fetches inbox metadata in pages (`GMAIL_FETCH_EMAILS`, no full
   bodies) up to the configured max.
2. Each message is run through the rule engine first; only unresolved
   messages go to Gemini, in batches, with results cached by
   sender+subject.
3. `safety_rules.decide_action` turns (category, confidence, age) into a
   recommended action, applying protections first.
4. The dashboard lets the user override the recommendation per category.
5. Preview and Confirmation show exactly what will happen; moving anything
   to Trash requires a separate explicit checkbox.
6. Execute Cleanup applies archive (batched, up to 1000/request) and trash
   (looped, Gmail has no bulk-trash endpoint) actions, retrying transient
   and rate-limit errors with backoff, and logs every action to the audit
   CSV.

## Safety Controls

- Categories that can never be auto-trashed: Important, Requires Action,
  Finance, Invoice/Receipt, Client, Personal, Uncertain.
- Keyword net (bank, tax, invoice, payroll, legal, government, security
  alert, etc.) protects matching mail regardless of classified category.
- OTP/verification and password-reset mail is protected if received within
  the last 72 hours.
- Confidence < `CONFIDENCE_LOW` -> always KEEP.
- `CONFIDENCE_LOW` <= confidence < `CONFIDENCE_HIGH` -> REVIEW, never
  auto-applied.
- Only `Version 1` actions exist: KEEP, ARCHIVE, TRASH (=Gmail Trash, which
  Gmail retains for 30 days - not permanent delete), ADD_LABEL.
- TRASH requires a separate explicit checkbox confirmation beyond the
  general Execute Cleanup click.
- Everything is gated behind Dry Run until the operator turns it off.

## Troubleshooting

- **"Missing configuration" banner** - check `.env` has all four required
  keys set and `streamlit run` was launched from a shell where `.env` is in
  the working directory.
- **Connect Gmail does nothing / errors** - verify
  `COMPOSIO_GMAIL_AUTH_CONFIG_ID` matches an auth config in your Composio
  project and that the Google OAuth client has the Gmail API enabled.
- **Status stuck on INITIATED** - the user hasn't completed the Google
  consent screen yet, or closed it before finishing; click Connect Gmail
  again to get a fresh link.
- **Classification looks wrong** - check `data/cache/classification_cache.json`;
  delete it to force re-classification if senders' behavior changed.
- **Rate limit errors during cleanup** - the app retries with exponential
  backoff automatically; persistent failures show up in the Results panel's
  Errors count and in the audit CSV.

## Known Limitations

- Gmail has no bulk-trash API; trashing large volumes is done one message
  at a time and will be slower than archiving.
- The rule engine's sender/domain lists are a starting point, not
  exhaustive - unmatched mail correctly falls through to Gemini rather than
  guessing.
- No support yet for label-based custom user rules beyond the built-in
  category -> action mapping (configurable, but through `.env`/`config.py`,
  not yet a UI rule builder).
- Single Gmail account per session; multi-account switching isn't
  implemented in v1.

## Privacy / Security Considerations

- Composio and Gemini API keys are read only from environment variables,
  never rendered in the UI or written to logs.
- Audit logs store sender, subject, classification, and action - never full
  message bodies.
- Full message body is fetched only via `gmail_service.fetch_full_body`,
  which nothing in the current classification path calls by default.
- Each user's Gmail OAuth token is managed by Composio under their own
  connected account; this app never sees or stores raw Gmail credentials.
- Moving to Trash is recoverable (Gmail retains Trash for 30 days);
  permanent delete (`GMAIL_BATCH_DELETE_MESSAGES`) is intentionally not
  wired up in v1.
