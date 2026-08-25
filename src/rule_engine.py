"""Deterministic classification rules, applied before any Gemini API call.

Goal: resolve the obvious majority of inbox mail (OTPs, security alerts,
transaction alerts, promos, ...) for free and instantly, so Gemini is only
spent on genuinely ambiguous mail.

Classification here is purpose-first: `_detect_purpose` looks at subject +
snippet content to decide what an email is FOR (category/detailed_type),
completely independent of who sent it. Sender is only ever used to derive
`context` (Level-2, e.g. "Banking", "Amazon") - a display/grouping label
that never influences the Level-1 category. This is what lets a single
sender (a bank, Amazon, GitHub...) land in many different categories:
purpose > content > subject > sender.
"""
from __future__ import annotations

import re

from src.config import Category, compute_cleanup_safe
from src.models import ClassificationResult, EmailMetadata

RULE_CONFIDENCE = 0.95

# ---------------------------------------------------------------------------
# Level-2 context: sender/domain -> display label. Never used for `category`.
# ---------------------------------------------------------------------------

_CONTEXT_SUBSTRING_MAP: list[tuple[str, str]] = [
    ("amazon", "Amazon"),
    ("github", "GitHub"),
    ("accounts.google.com", "Google"),
    ("google.com", "Google"),
    ("linkedin", "LinkedIn"),
    ("facebookmail", "Facebook"),
    ("facebook.com", "Facebook"),
    ("instagram", "Instagram"),
    ("twitter.com", "Twitter/X"),
    ("@x.com", "Twitter/X"),
    ("stripe.com", "Stripe"),
    ("paypal.com", "PayPal"),
    ("slack.com", "Slack"),
    ("substack.com", "Substack"),
    ("mailchimp.com", "Mailchimp"),
    ("medium.com", "Medium"),
]

# Bank name / keyword fragments that identify the "Banking" context, without
# forcing any particular Level-1 category - purely a display label.
_BANK_KEYWORDS = [
    "bank", "idfc", "hdfc", "icici", "sbi", "axis", "kotak", "indusind",
    "chase", "citibank", "wellsfargo", "barclays", "hsbc",
    "standardchartered", "yesbank", "pnb", "rbc", "boa",
]

_SUBDOMAIN_PREFIXES = (
    "no-reply.", "noreply.", "notifications.", "notification.", "mail.",
    "email.", "updates.", "alerts.", "e.", "info.",
)


def derive_context(sender: str) -> str:
    """Best-effort Level-2 label for a sender. Never raises."""
    sender_lower = sender.lower()

    for needle, label in _CONTEXT_SUBSTRING_MAP:
        if needle in sender_lower:
            return label

    for keyword in _BANK_KEYWORDS:
        if keyword in sender_lower:
            return "Banking"

    match = re.search(r"@([\w.-]+)", sender_lower)
    if not match:
        return "Unknown"
    domain = match.group(1)
    for prefix in _SUBDOMAIN_PREFIXES:
        if domain.startswith(prefix):
            domain = domain[len(prefix):]
    label = domain.split(".")[0] if domain else ""
    if not label:
        return "Unknown"
    return label.replace("-", " ").replace("_", " ").title()


# ---------------------------------------------------------------------------
# Level-1 purpose ladder: content/subject first, sender-domain as tiebreak.
# ---------------------------------------------------------------------------

_OTP_RE = re.compile(r"\b(otp|verification code|one[- ]time password|security code)\b", re.I)

_SECURITY_RE = re.compile(
    r"\b(unusual login|new device sign-?in|unauthorized (login|access|transaction)|"
    r"suspicious activity|fraud alert|account (has been )?compromised|"
    r"(login|sign-?in) attempt|password (was |has been )?(reset|changed)|"
    r"2-step verification|security alert|verify your identity|account recovery|"
    r"new oauth application|new ssh key|api token (created|generated)|"
    r"added (a )?new device)\b",
    re.I,
)

_PROMO_RE = re.compile(
    r"\b(exclusive offer|limited[- ]time offer|apply now|pre-?approved|special deal|"
    r"\d+% off|cashback offer|shop now|book now|insurance offer|get a loan|"
    r"get a credit card|upgrade (now|today)|discount|deal of the day|"
    r"offer expires|save up to|sale ends)\b",
    re.I,
)

_TRANSACTION_RE = re.compile(
    r"\b(debited|credited|debit alert|credit alert|has been (debited|credited)|"
    r"upi (payment|transaction)|transaction (of|alert)|"
    r"payment (of|successful|received)|amount (debited|credited|paid)|"
    r"withdrawn|charged to your card)\b",
    re.I,
)

_STATEMENT_RE = re.compile(
    r"\b(account statement|monthly statement|e-?statement|credit card statement|"
    r"statement is ready|statement for)\b",
    re.I,
)

_BILLS_RE = re.compile(
    r"\b(payment due|bill due|amount due|emi due|minimum (amount )?due|"
    r"pay by \w|installment due|loan (emi|payment) (due|reminder))\b",
    re.I,
)

_ORDERS_RE = re.compile(
    r"\b(order (confirmed|confirmation|placed|#)|your order|invoice #|"
    r"invoice attached|receipt for your (order|purchase)|purchase confirmation)\b",
    re.I,
)

_SHIPPING_RE = re.compile(
    r"\b(shipped|out for delivery|has been delivered|tracking number|"
    r"arriving (today|tomorrow|on)|on its way|dispatch(ed)?)\b",
    re.I,
)

_ACCOUNT_UPDATE_RE = re.compile(
    r"\b(complete your kyc|kyc (pending|required|update)|"
    r"update your (account|profile|details)|terms of service (update|changed)|"
    r"policy update|verify your account|account (update|notice)|"
    r"service notification)\b",
    re.I,
)

_SUBSCRIPTION_RE = re.compile(
    r"\b(subscription (renewed|renewal|expiring)|your subscription|auto-renew|"
    r"free trial (ending|ends)|membership renewal)\b",
    re.I,
)

_APP_NOTIFICATION_RE = re.compile(
    r"\b(pull request|merged|issue #\d+|new commit|build (passed|failed)|"
    r"mentioned you|comment(ed)? on|deployment (succeeded|failed)|"
    r"workflow run)\b",
    re.I,
)

_NEWSLETTER_RE = re.compile(r"\b(newsletter|weekly digest|this week in)\b", re.I)

_SOCIAL_RE = re.compile(
    r"\b(liked your|commented on your|new connection request|tagged you|new follower)\b",
    re.I,
)

_TRAVEL_RE = re.compile(
    r"\b(boarding pass|itinerary|flight (confirmation|status)|"
    r"check-?in for your flight|reservation confirmed|hotel booking)\b",
    re.I,
)

_NEWSLETTER_DOMAINS = ("mailchimp.com", "substack.com", "medium.com")
_SOCIAL_DOMAINS = ("linkedin.com", "facebookmail.com", "twitter.com", "@x.com", "instagram.com")
_APP_NOTIFICATION_DOMAINS = ("slack.com", "notifications.github.com")


def _detect_purpose(subject: str, snippet: str, sender_lower: str) -> tuple[str, str, bool, bool] | None:
    """Returns (category, detailed_type, is_promotional, is_security_sensitive)
    or None if no rule confidently matches. Order encodes purpose priority:
    security/verification mail is checked before promotional mail, which is
    checked before generic transactional/informational mail, so e.g. a
    promo phrase can't be shadowed by an incidental mention of "payment"."""
    text = f"{subject} {snippet}"

    if _OTP_RE.search(text):
        return Category.OTP_VERIFICATION, "OTP", False, True

    if _SECURITY_RE.search(text):
        if "fraud" in text.lower() or "compromised" in text.lower():
            detailed = "Fraud Alert"
        elif "login" in text.lower() or "sign-in" in text.lower() or "sign in" in text.lower():
            detailed = "Login Alert"
        else:
            detailed = "Security Alert"
        return Category.SECURITY_ALERTS, detailed, False, True

    if _PROMO_RE.search(text):
        lower = text.lower()
        if "loan" in lower:
            detailed = "Loan Promotion"
        elif "credit card" in lower:
            detailed = "Credit Card Promotion"
        elif "insurance" in lower:
            detailed = "Insurance Promotion"
        else:
            detailed = "Promotional Offer"
        return Category.PROMOTIONS_MARKETING, detailed, True, False

    if _TRANSACTION_RE.search(text):
        lower = text.lower()
        if "upi" in lower:
            detailed = "UPI Transaction"
        elif "card" in lower:
            detailed = "Card Transaction"
        elif "credited" in lower:
            detailed = "Credit Alert"
        elif "debited" in lower:
            detailed = "Debit Alert"
        else:
            detailed = "Transaction Alert"
        return Category.TRANSACTIONS, detailed, False, False

    if _STATEMENT_RE.search(text):
        detailed = "Credit Card Statement" if "credit card" in text.lower() else "Statement"
        return Category.STATEMENTS_DOCUMENTS, detailed, False, False

    if _BILLS_RE.search(text):
        lower = text.lower()
        detailed = "EMI / Loan Payment" if ("emi" in lower or "loan" in lower or "installment" in lower) else "Payment Due"
        return Category.BILLS_PAYMENTS, detailed, False, False

    if _ORDERS_RE.search(text):
        return Category.ORDERS_PURCHASES, "Order Confirmation", False, False

    if _SHIPPING_RE.search(text):
        return Category.SHIPPING_DELIVERY, "Delivery Update", False, False

    if _ACCOUNT_UPDATE_RE.search(text):
        detailed = "KYC" if "kyc" in text.lower() else "Account Update"
        return Category.ACCOUNT_SERVICE_UPDATES, detailed, False, False

    if _SUBSCRIPTION_RE.search(text):
        return Category.SUBSCRIPTIONS, "Subscription Renewal", False, False

    if _APP_NOTIFICATION_RE.search(text):
        return Category.APP_SYSTEM_NOTIFICATIONS, "System Notification", False, False

    if _TRAVEL_RE.search(text):
        return Category.TRAVEL, "Travel Itinerary", False, False

    if _NEWSLETTER_RE.search(text) or any(d in sender_lower for d in _NEWSLETTER_DOMAINS):
        return Category.NEWSLETTERS, "Newsletter", False, False

    if _SOCIAL_RE.search(text) or any(d in sender_lower for d in _SOCIAL_DOMAINS):
        return Category.SOCIAL, "Social Notification", False, False

    if any(d in sender_lower for d in _APP_NOTIFICATION_DOMAINS):
        return Category.APP_SYSTEM_NOTIFICATIONS, "System Notification", False, False

    return None


def classify_by_rules(email: EmailMetadata) -> ClassificationResult | None:
    """Return a rule-based classification, or None if no rule matches
    confidently enough (caller should send it to Gemini instead)."""
    sender_lower = email.sender.lower()
    context = derive_context(email.sender)

    purpose = _detect_purpose(email.subject, email.snippet, sender_lower)
    if purpose:
        category, detailed_type, is_promotional, is_security_sensitive = purpose
        reason = f"Matched purpose rule: {detailed_type}"
        return _result(email, category, reason, context, detailed_type, is_promotional, is_security_sensitive)

    if email.unsubscribe_header:
        return _result(
            email, Category.PROMOTIONS_MARKETING,
            "Has List-Unsubscribe header (marketing indicator)", context,
            "Promotional Offer", is_promotional=True, is_security_sensitive=False,
            confidence=0.80,
        )

    return None


def _result(
    email: EmailMetadata,
    category: str,
    reason: str,
    context: str,
    detailed_type: str,
    is_promotional: bool,
    is_security_sensitive: bool,
    confidence: float = RULE_CONFIDENCE,
) -> ClassificationResult:
    return ClassificationResult(
        message_id=email.message_id,
        category=category,
        confidence=confidence,
        reason=reason,
        source="rule",
        context=context,
        detailed_type=detailed_type,
        is_promotional=is_promotional,
        is_security_sensitive=is_security_sensitive,
        cleanup_safe=compute_cleanup_safe(category, is_promotional, is_security_sensitive),
    )
