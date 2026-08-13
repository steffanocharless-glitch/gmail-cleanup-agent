"""Safety gate: decides the final recommended action for a classified email.

This module is the single place that is allowed to say TRASH. Every other
module must route decisions through `decide_action`.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.config import Action, AppConfig, Category, PROTECTED_KEYWORDS
from src.models import ClassificationResult, EmailMetadata, RecommendedAction

RECENT_SECURITY_WINDOW_HOURS = 72


def decide_action(
    email: EmailMetadata,
    classification: ClassificationResult,
    config: AppConfig,
) -> RecommendedAction:
    """Apply protection rules, confidence thresholds, and age rules, in that
    order, to produce a single recommended action. Protection always wins."""

    protected, protection_reason = _is_protected(email, classification, config)
    if protected:
        return RecommendedAction(
            message_id=email.message_id,
            email=email,
            classification=classification,
            recommended_action=Action.KEEP,
            protected=True,
            protection_reason=protection_reason,
        )

    band = config.thresholds.band(classification.confidence)
    if band == "auto_keep":
        return RecommendedAction(
            message_id=email.message_id,
            email=email,
            classification=classification,
            recommended_action=Action.KEEP,
            protected=False,
            protection_reason=f"Confidence {classification.confidence:.2f} below low threshold "
                               f"({config.thresholds.low}); defaulting to KEEP",
        )

    base_action = config.category_actions.get(classification.category, Action.REVIEW)

    if band == "manual_review":
        recommended = Action.REVIEW if base_action != Action.KEEP else Action.KEEP
        return RecommendedAction(
            message_id=email.message_id,
            email=email,
            classification=classification,
            recommended_action=recommended,
        )

    # band == "high": deterministic action, but still gate TRASH/ARCHIVE by age rule.
    if base_action in (Action.ARCHIVE, Action.TRASH):
        min_age = config.age_rules_days.get(classification.category)
        if min_age is not None and email.age_days < min_age:
            return RecommendedAction(
                message_id=email.message_id,
                email=email,
                classification=classification,
                recommended_action=Action.KEEP,
                protection_reason=f"Only {email.age_days}d old; age rule requires {min_age}d",
            )

    return RecommendedAction(
        message_id=email.message_id,
        email=email,
        classification=classification,
        recommended_action=base_action,
    )


def _is_protected(
    email: EmailMetadata, classification: ClassificationResult, config: AppConfig
) -> tuple[bool, str | None]:
    if classification.category in config.protected_categories:
        return True, f"Category '{classification.category}' is protected"

    if classification.confidence < config.thresholds.low:
        return True, f"Low confidence ({classification.confidence:.2f})"

    haystack = f"{email.sender} {email.subject} {email.snippet}".lower()
    for keyword in PROTECTED_KEYWORDS:
        if keyword in haystack:
            return True, f"Matched protected keyword: '{keyword}'"

    if classification.category == Category.OTP_VERIFICATION or "password reset" in haystack:
        received = email.received_at
        now = datetime.now(received.tzinfo or timezone.utc)
        if now - received < timedelta(hours=RECENT_SECURITY_WINDOW_HOURS):
            return True, "Recent security/verification email (within 72h)"

    return False, None


def requires_trash_confirmation(recommended_action: str) -> bool:
    return recommended_action == Action.TRASH
