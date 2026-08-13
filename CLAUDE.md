# CLAUDE.md

Guidance for AI agents (Claude Code or similar) working in this repository.

## What this project is

Streamlit app that lets a user connect their own Gmail account through
Composio OAuth, classifies inbox mail (rule engine first, Claude for the
rest), and performs an archive/trash/label cleanup only after explicit user
confirmation. Safety-critical: `src/safety_rules.py` is the single gate
allowed to recommend `TRASH`, and Dry Run defaults to on.

## Non-negotiable invariants

- Never add a code path that trashes or permanently deletes mail without
  going through `safety_rules.decide_action` and a UI confirmation step.
- Never widen `PROTECTED_CATEGORIES` / `PROTECTED_KEYWORDS` removal without
  explicit user instruction - these exist to stop financial/legal/security
  mail from being auto-deleted.
- Never store full email bodies in `data/cleanup_logs/` audit CSVs.
- Never hard-code confidence thresholds or age-rule day counts outside
  `src/config.py`.
- Keep `DRY_RUN_DEFAULT=true` as the shipped default.
- Do not commit `.env`, anything under `data/cleanup_logs/`, or
  `data/cache/` - all are gitignored on purpose.

## Architecture quick reference

- `composio_service.py` - only module that talks to the Composio SDK
  directly. All retry/backoff logic for Gmail tool calls lives here.
- `gmail_service.py` - Gmail-specific operations (fetch/archive/trash/label)
  built on top of `composio_service`. No Composio SDK imports outside this
  file and `composio_service.py`.
- `rule_engine.py` - free, deterministic classification. Add new sender/
  domain/subject rules here before reaching for Claude.
- `classifier.py` - Claude classification with caching by
  sha256(sender|subject). Keep the system prompt's category list in sync
  with `config.Category.ALL`.
- `safety_rules.py` - the only place decisions become `KEEP`/`ARCHIVE`/
  `TRASH`/`REVIEW`.
- `cleanup_engine.py` - orchestration only; no business rules should live
  here beyond wiring the above together and writing audit records.

## Testing

Run `pytest -q` from the project root (inside the venv). Safety-rule and
cleanup-engine tests are the ones that matter most here - if you touch
`safety_rules.py` or `cleanup_engine.py`, run the full suite before
considering the change done.

## When extending Gmail actions

Confirm the current Composio Python SDK tool slugs/parameters before using
them (they have changed before - `composio-core` on PyPI is deprecated,
`composio` is current as of this writing). Do not assume a bulk-trash tool
exists; as of this writing Gmail's API has no such endpoint and
`gmail_service.trash_batch` loops one call per message.
