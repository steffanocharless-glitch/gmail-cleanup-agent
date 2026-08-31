"""Detect actionable dates (bill due, renewal, interview, deadline...) in an
email's subject/snippet.

Deliberately independent of the Level-1 purpose classification in
rule_engine.py - this is a content-based add-on layer, not a reclassification.
Trigger phrase presence decides "is this actionable at all"; date parsing
decides "when". Never invents a date with false confidence: if a trigger
phrase fires but no date can be parsed nearby, a placeholder is returned
flagged `date_is_certain=False` so the UI forces a review/edit instead of
silently guessing or silently dropping a real deadline.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from dateutil import parser as dateutil_parser

from src.models import EmailMetadata

DEFAULT_UNCERTAIN_OFFSET_DAYS = 7

_ACTIONABLE_PHRASE_RE = re.compile(
    r"\b(due date|due by|payment due|deadline|last date to|renews? on|renewal date|"
    r"expir(?:es|ing) on|interview (?:scheduled|on|date)|appointment(?: on| scheduled)?|"
    r"meeting (?:scheduled|on)|application deadline|reminder:)\b",
    re.I,
)

_DATE_CANDIDATE_RE = re.compile(
    r"\b(\d{1,2}[./-]\d{1,2}[./-]\d{2,4}"
    r"|\d{4}-\d{1,2}-\d{1,2}"
    r"|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:,?\s*\d{4})?"
    r"|\d{1,2}(?:st|nd|rd|th)?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?(?:\s+\d{4})?)\b",
    re.I,
)

_HAS_YEAR_RE = re.compile(r"\d{4}")


@dataclass
class ActionableSuggestion:
    message_id: str
    summary: str
    suggested_date: date
    date_is_certain: bool


def detect_actionable_date(email: EmailMetadata) -> ActionableSuggestion | None:
    text = f"{email.subject} {email.snippet}"
    if not _ACTIONABLE_PHRASE_RE.search(text):
        return None

    for match in _DATE_CANDIDATE_RE.finditer(text):
        candidate = match.group(0)
        has_year = bool(_HAS_YEAR_RE.search(candidate))
        # dateutil fills in any field the candidate text doesn't specify
        # (here: year) from this default rather than an implicit/unreliable
        # fallback - a year-less candidate resolves against the email's own
        # received year, not whatever the parsing library defaults to.
        default = datetime(email.received_at.year, 1, 1)
        try:
            parsed = dateutil_parser.parse(candidate, dayfirst=True, default=default)
        except (ValueError, OverflowError, dateutil_parser.ParserError):
            continue
        result = parsed.date()
        # A year-less date ("September 3") that lands before the email's own
        # received date almost always means "next occurrence", not the past.
        if not has_year and result < email.received_at.date():
            result = result.replace(year=result.year + 1)
        return ActionableSuggestion(
            message_id=email.message_id,
            summary=_summarize(email),
            suggested_date=result,
            date_is_certain=True,
        )

    fallback = email.received_at.date() + timedelta(days=DEFAULT_UNCERTAIN_OFFSET_DAYS)
    return ActionableSuggestion(
        message_id=email.message_id,
        summary=_summarize(email),
        suggested_date=fallback,
        date_is_certain=False,
    )


def _summarize(email: EmailMetadata) -> str:
    return f"{email.sender}: {email.subject}"[:200]
