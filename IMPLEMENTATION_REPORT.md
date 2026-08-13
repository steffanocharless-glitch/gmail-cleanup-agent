# Implementation Report - AI Gmail Cleanup Agent

## What Was Built

A standalone Streamlit application, `gmail-cleanup-agent/`, that lets any
user (not just the developer) connect their own Gmail account through
Composio-hosted OAuth, scan their inbox, get AI-assisted classification into
14 categories, review and override recommended cleanup actions, and execute
a safe cleanup (archive / move to Trash / add label) only after explicit
confirmation. Dry Run is on by default; no Gmail account was ever mutated
during development.

## Architecture

```
gmail-cleanup-agent/
  app.py                  Streamlit UI/orchestration
  src/
    config.py             Categories, actions, protected lists, thresholds, age rules
    models.py              EmailMetadata, ClassificationResult, RecommendedAction, ...
    logger.py               App logger + per-user CSV AuditLogger
    composio_service.py    Composio SDK wrapper: link/wait/status/disable, tools.execute w/ retry
    gmail_service.py        Gmail ops on top of composio_service: fetch, archive, trash, labels
    rule_engine.py          Deterministic sender/domain/subject classification
    classifier.py           Claude-based classification, batched + cached, usage tracking
    safety_rules.py         Sole gate for KEEP/ARCHIVE/TRASH/REVIEW decisions
    cleanup_engine.py       scan_and_classify() and execute_cleanup() orchestration
  tests/                   24 pytest tests
  data/cleanup_logs/       Per-user, per-day CSV audit trail (gitignored)
  data/cache/               Classification cache keyed by sha256(sender|subject) (gitignored)
  .env.example, .gitignore, requirements.txt, README.md, CLAUDE.md
```

## Files Created

`app.py`; `src/config.py`, `src/models.py`, `src/logger.py`,
`src/composio_service.py`, `src/gmail_service.py`, `src/rule_engine.py`,
`src/classifier.py`, `src/safety_rules.py`, `src/cleanup_engine.py`,
`src/__init__.py`; `tests/conftest.py`, `tests/test_rule_engine.py`,
`tests/test_safety_rules.py`, `tests/test_classifier.py`,
`tests/test_cleanup_engine.py`, `tests/__init__.py`; `requirements.txt`,
`.env.example`, `.gitignore`, `README.md`, `CLAUDE.md`, this report.

## Authentication Flow

1. `ComposioService.__init__` creates a `Composio(api_key=...)` client -
   `COMPOSIO_API_KEY` is read once from env, never exposed to the UI.
2. User clicks **Connect Gmail** -> `ComposioService.start_connection()`
   calls `composio.connected_accounts.link(user_id, auth_config_id,
   callback_url)` (current SDK method; the older `initiate()` is deprecated
   for Composio-managed OAuth and was deliberately avoided).
3. The returned `redirect_url` is shown to the user; they complete Google's
   consent screen themselves, authorizing *their own* mailbox against the
   shared `COMPOSIO_GMAIL_AUTH_CONFIG_ID`.
4. User clicks **I've authorized - check status** ->
   `wait_for_connection(connected_account_id, timeout=15)` polls Composio
   until the connection is `ACTIVE`.
5. `user_id` is a random UUID generated per Streamlit session
   (`st.session_state`), so concurrent users/sessions never collide and no
   one reuses another person's connection or the developer's own account.

## Gmail Flow

- **Fetch**: `GMAIL_FETCH_EMAILS` paginated via `page_token`, metadata-only
  (`include_payload: False`) - sender, subject, snippet, label IDs,
  timestamp, attachment flag. Full body (`GMAIL_FETCH_MESSAGE_BY_ID`) is
  only ever called if something explicitly invokes
  `GmailService.fetch_full_body` - nothing in the current classification
  path does, keeping token/data usage minimal.
- **Archive**: `GMAIL_BATCH_MODIFY_MESSAGES` removing the `INBOX` label,
  batched up to 1000 message IDs per call.
- **Trash**: `GMAIL_MOVE_TO_TRASH`, looped per message - Gmail's API has no
  bulk-trash endpoint (confirmed via Composio's Gmail toolkit action list).
- **Labels**: `GMAIL_LIST_LABELS` / `GMAIL_CREATE_LABEL` /
  `GMAIL_BATCH_MODIFY_MESSAGES` (add path) for `ADD_LABEL` actions.
- **Retries**: `ComposioService.execute()` retries transient/HTTP-429
  failures with exponential backoff (`GMAIL_RETRY_ATTEMPTS`,
  `GMAIL_RETRY_BACKOFF_SECONDS`), and batch operations collect and report
  per-chunk failures instead of aborting the whole run.

## Classification Logic

1. `rule_engine.classify_by_rules()` runs first, for free: known sender
   addresses/domains (Google verification, Mailchimp/Substack, LinkedIn/
   Slack/GitHub notifications, Stripe/PayPal), subject-keyword regexes (OTP,
   invoice/receipt, promotional, security), and a `List-Unsubscribe` header
   fallback to Promotional.
2. Anything unresolved goes to `classifier.EmailClassifier`, which batches
   (`CLASSIFY_BATCH_SIZE`, default 20) metadata-only payloads to Claude
   (`claude-sonnet-5` by default), asking for strict JSON output against the
   fixed 14-category list. Results are cached by
   `sha256(sender|subject)` in `data/cache/classification_cache.json` so
   re-scans of the same sender/subject pairs cost zero additional tokens.
   Any parse failure, invalid category, or API error defaults the message to
   `Uncertain` at confidence `0.0` rather than guessing.
3. `safety_rules.decide_action()` converts (category, confidence, age) into
   a final recommendation:
   - Protected category, protected keyword match, confidence below the low
     threshold, or a security/OTP email within 72 hours -> `KEEP`
     (protected), unconditionally.
   - Confidence in the "manual review" band -> `REVIEW`.
   - High-confidence Archive/Trash candidates still respect the per-category
     age rule (e.g. Promotional must be 30+ days old) before being
     recommended - otherwise `KEEP`.

## Safety Mechanisms

- Hard-coded `PROTECTED_CATEGORIES` (Important, Requires Action, Finance,
  Invoice/Receipt, Client, Personal, Uncertain) can never resolve to `TRASH`
  regardless of confidence.
- `PROTECTED_KEYWORDS` keyword net catches financial/legal/HR/security
  language even if the classifier mis-categorizes the email.
- Confidence thresholds (`CONFIDENCE_HIGH=0.90`, `CONFIDENCE_LOW=0.70`,
  configurable via env) gate auto-action vs. manual review vs. forced keep.
- Dry Run defaults on; disabling it is a conscious, per-session UI action.
- Trash requires a second, explicit checkbox beyond the general Execute
  Cleanup confirmation, and `cleanup_engine.execute_cleanup` refuses to call
  `trash_batch` unless `trash_confirmed=True`.
- Trash uses Gmail's Trash (30-day recoverable), never
  `GMAIL_BATCH_DELETE_MESSAGES` (permanent delete) - that tool is not wired
  up anywhere in v1.
- Audit CSV records every decision (including skipped/dry-run/manual-review
  ones) with classification, confidence, recommended vs. final action, and
  reason - but never the email body.

## Tests Run

`pytest -q` from the project venv.

```
tests/test_classifier.py .... 
tests/test_cleanup_engine.py ......
tests/test_rule_engine.py .....
tests/test_safety_rules.py .........
24 passed in 3.53s
```

Coverage highlights:
- Financial/important/uncertain/low-confidence emails are never
  recommended for Trash (`test_safety_rules.py`).
- Age-rule gating (promotional under/over threshold) behaves correctly.
- Dry Run performs zero Gmail service calls (`test_dry_run_performs_no_gmail_mutation`).
- Trash without the confirmation flag makes zero `trash_batch` calls.
- Batch archive/trash failures are counted as errors, not silently dropped.
- Classifier falls back to `Uncertain` on invalid model output or API
  errors, and reuses cache instead of re-calling Claude for a
  previously-seen sender/subject pair.

Composio and Anthropic API calls were mocked throughout - no real Gmail
account was scanned, classified, or mutated during development or testing,
per the constraint against real Gmail deletion during dev.

## Verification Performed

- `ast.parse` syntax check across all source files - clean.
- `python -c "import app"` - imports cleanly with placeholder env vars.
- `streamlit run app.py --server.headless true` launched successfully on a
  local port, returned HTTP 200, and the server log showed no exceptions or
  tracebacks (verified with a throwaway `.env` containing placeholder keys,
  then deleted).
- Confirmed no `.env`, `data/cleanup_logs/*.csv`, or `data/cache/*.json`
  exist in the working tree pre-commit; `.gitignore` excludes all of them
  plus `venv/`.

## Remaining Limitations

- No bulk-trash Gmail endpoint exists, so trashing large volumes is
  necessarily sequential and slower than archiving.
- Rule-engine sender/domain lists are a reasonable starting set, not
  exhaustive; expect a meaningful share of mail to still route through
  Claude on a fresh inbox until senders repeat and hit cache.
- Single Gmail connection per browser session; no multi-account switcher in
  v1.
- Category -> action defaults and age rules are configurable via
  `src/config.py` / `.env`, but there is no in-app rule-builder UI yet.
- `link()`'s exact Python signature was reconstructed from Composio's
  GitHub source and docs (the hosted docs page for `link()` was missing a
  Python code sample at the time of writing); verify against
  `docs.composio.dev` if Composio ships a breaking SDK change before you
  deploy.

## Exact Commands To Run The Project

```bash
git clone <this-repo-url> gmail-cleanup-agent
cd gmail-cleanup-agent
python -m venv venv
# Windows:
venv\Scripts\activate
# macOS/Linux:
source venv/bin/activate

pip install -r requirements.txt
cp .env.example .env
# edit .env: COMPOSIO_API_KEY, COMPOSIO_GMAIL_AUTH_CONFIG_ID, ANTHROPIC_API_KEY

streamlit run app.py
```

Then open the printed local URL, click **Connect Gmail**, authorize your own
account, **Scan Inbox**, review recommendations, and only disable Dry Run /
confirm Trash when ready for real changes.

Run tests any time with:

```bash
pytest -q
```
