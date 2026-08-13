"""Deterministic classification rules, applied before any Gemini API call.

Goal: resolve the obvious majority of inbox mail (known newsletter senders,
OTP senders, marketing footers) for free and instantly, so Gemini is only
spent on genuinely ambiguous mail.
"""
from __future__ import annotations

import re

from src.config import Category
from src.models import ClassificationResult, EmailMetadata

# High-confidence sender-domain -> category rules.
DOMAIN_RULES: dict[str, str] = {
    "noreply@accounts.google.com": Category.OTP_VERIFICATION,
    "no-reply@accounts.google.com": Category.OTP_VERIFICATION,
}

DOMAIN_SUFFIX_RULES: dict[str, str] = {
    "@mailchimp.com": Category.NEWSLETTER,
    "@substack.com": Category.NEWSLETTER,
    "@medium.com": Category.NEWSLETTER,
    "@linkedin.com": Category.SOCIAL,
    "@facebookmail.com": Category.SOCIAL,
    "@twitter.com": Category.SOCIAL,
    "@x.com": Category.SOCIAL,
    "@instagram.com": Category.SOCIAL,
    "@slack.com": Category.AUTOMATED_NOTIFICATION,
    "@notifications.github.com": Category.AUTOMATED_NOTIFICATION,
    "@stripe.com": Category.FINANCE,
    "@paypal.com": Category.FINANCE,
}

SUBJECT_KEYWORD_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bverification code\b|\botp\b|\bone-time password\b", re.I), Category.OTP_VERIFICATION),
    (re.compile(r"\binvoice\b|\breceipt\b|\bpayment confirmation\b|\border confirmation\b", re.I), Category.INVOICE_RECEIPT),
    (re.compile(r"\bunsubscribe\b|\b% off\b|\bsale\b|\blimited time\b|\bdeal\b", re.I), Category.PROMOTIONAL),
    (re.compile(r"\bpassword reset\b|\bsecurity alert\b|\bsign-?in attempt\b|\bnew device\b", re.I), Category.IMPORTANT),
]

RULE_CONFIDENCE = 0.95


def classify_by_rules(email: EmailMetadata) -> ClassificationResult | None:
    """Return a rule-based classification, or None if no rule matches."""
    sender_lower = email.sender.lower()

    if sender_lower in DOMAIN_RULES:
        category = DOMAIN_RULES[sender_lower]
        return _result(email, category, "Known sender address")

    for suffix, category in DOMAIN_SUFFIX_RULES.items():
        if suffix in sender_lower:
            return _result(email, category, f"Sender domain matches {suffix}")

    for pattern, category in SUBJECT_KEYWORD_RULES:
        if pattern.search(email.subject):
            return _result(email, category, f"Subject matches pattern: {pattern.pattern}")

    if email.unsubscribe_header:
        return _result(
            email, Category.PROMOTIONAL, "Has List-Unsubscribe header (marketing indicator)",
            confidence=0.80,
        )

    return None


def _result(email: EmailMetadata, category: str, reason: str, confidence: float = RULE_CONFIDENCE) -> ClassificationResult:
    return ClassificationResult(
        message_id=email.message_id,
        category=category,
        confidence=confidence,
        reason=reason,
        source="rule",
    )
