"""Orchestrates scan -> classify -> recommend -> (dry-run | execute)."""
from __future__ import annotations

from src.classifier import EmailClassifier
from src.config import Action, AppConfig
from src.gmail_service import GmailService
from src.logger import AuditLogger, get_logger
from src.models import CleanupSummary, EmailMetadata, RecommendedAction, ScanResult
from src.rule_engine import classify_by_rules
from src.safety_rules import decide_action

logger = get_logger(__name__)


def scan_and_classify(
    gmail: GmailService,
    classifier: EmailClassifier,
    config: AppConfig,
) -> ScanResult:
    emails: list[EmailMetadata] = gmail.fetch_inbox_metadata(config.max_messages_per_scan)
    profile = gmail.get_profile()

    rule_hits, needs_gemini = [], []
    for email in emails:
        result = classify_by_rules(email)
        if result:
            rule_hits.append((email, result))
        else:
            needs_gemini.append(email)

    gemini_results = classifier.classify(needs_gemini) if needs_gemini else []
    gemini_by_id = {r.message_id: r for r in gemini_results}

    recommendations: list[RecommendedAction] = []
    for email, result in rule_hits:
        recommendations.append(decide_action(email, result, config))
    for email in needs_gemini:
        result = gemini_by_id.get(email.message_id)
        if result:
            recommendations.append(decide_action(email, result, config))

    dates = [e.received_at for e in emails]
    return ScanResult(
        scanned_count=len(emails),
        total_inbox_count=profile.get("messages_total") or profile.get("messagesTotal") or len(emails),
        unread_count=sum(1 for e in emails if e.is_unread),
        date_range_start=min(dates) if dates else None,
        date_range_end=max(dates) if dates else None,
        recommendations=recommendations,
    )


def execute_cleanup(
    gmail: GmailService,
    recommendations: list[RecommendedAction],
    config: AppConfig,
    audit: AuditLogger,
    trash_confirmed: bool,
    label_id: str | None = None,
) -> CleanupSummary:
    """Apply final_action (falls back to recommended_action) for each item.

    If config.dry_run is True, or trash_confirmed is False, no Gmail
    mutation is performed for TRASH items - they are logged as skipped.
    """
    summary = CleanupSummary(scanned=len(recommendations))
    to_archive, to_trash, to_label = [], [], []

    for rec in recommendations:
        action = rec.user_override or rec.final_action or rec.recommended_action
        summary.classified += 1

        if rec.protected:
            summary.protected += 1

        if action == Action.KEEP:
            summary.skipped += 1
            audit.log_action(
                rec.message_id, rec.email.sender, rec.email.subject,
                rec.classification.display_category, rec.classification.confidence,
                rec.recommended_action, Action.KEEP,
                rec.protection_reason or rec.classification.reason,
            )
            continue

        if action == Action.REVIEW:
            summary.manual_review += 1
            audit.log_action(
                rec.message_id, rec.email.sender, rec.email.subject,
                rec.classification.display_category, rec.classification.confidence,
                rec.recommended_action, Action.REVIEW, "Awaiting manual review",
            )
            continue

        if action == Action.TRASH and not trash_confirmed:
            summary.skipped += 1
            audit.log_action(
                rec.message_id, rec.email.sender, rec.email.subject,
                rec.classification.display_category, rec.classification.confidence,
                rec.recommended_action, "SKIPPED_UNCONFIRMED",
                "Trash requires explicit confirmation",
            )
            continue

        if config.dry_run:
            summary.skipped += 1
            audit.log_action(
                rec.message_id, rec.email.sender, rec.email.subject,
                rec.classification.display_category, rec.classification.confidence,
                rec.recommended_action, f"DRY_RUN_{action}",
                "Dry run - no mutation performed",
            )
            continue

        if action == Action.ARCHIVE:
            to_archive.append(rec)
        elif action == Action.TRASH:
            to_trash.append(rec)
        elif action == Action.ADD_LABEL:
            to_label.append(rec)

    if to_archive:
        failed = set(gmail.archive_batch([r.message_id for r in to_archive]))
        for rec in to_archive:
            ok = rec.message_id not in failed
            summary.archived += 1 if ok else 0
            summary.errors += 0 if ok else 1
            audit.log_action(
                rec.message_id, rec.email.sender, rec.email.subject,
                rec.classification.display_category, rec.classification.confidence,
                rec.recommended_action, Action.ARCHIVE if ok else "ERROR",
                rec.classification.reason, error=None if ok else "Batch archive failed",
            )

    if to_trash:
        failed = set(gmail.trash_batch([r.message_id for r in to_trash]))
        for rec in to_trash:
            ok = rec.message_id not in failed
            summary.trashed += 1 if ok else 0
            summary.errors += 0 if ok else 1
            audit.log_action(
                rec.message_id, rec.email.sender, rec.email.subject,
                rec.classification.display_category, rec.classification.confidence,
                rec.recommended_action, Action.TRASH if ok else "ERROR",
                rec.classification.reason, error=None if ok else "Trash failed",
            )

    if to_label and label_id:
        failed = set(gmail.add_label_batch([r.message_id for r in to_label], label_id))
        for rec in to_label:
            ok = rec.message_id not in failed
            summary.labeled += 1 if ok else 0
            summary.errors += 0 if ok else 1
            audit.log_action(
                rec.message_id, rec.email.sender, rec.email.subject,
                rec.classification.display_category, rec.classification.confidence,
                rec.recommended_action, Action.ADD_LABEL if ok else "ERROR",
                rec.classification.reason, error=None if ok else "Add label failed",
            )

    summary.error_details = []
    return summary
