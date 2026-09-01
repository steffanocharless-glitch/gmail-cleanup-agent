"""Builds concise alert text from already-classified data.

Deliberately never forwards a raw email body/snippet - only structured
fields (sender, category, detailed_type, a parsed date) that were already
extracted by the classification/actionable-date pipeline. Keeps messages
short and predictable regardless of which channel sends them - see
telegram_service.MAX_ALERT_LENGTH for the current channel's cap.
"""
from __future__ import annotations

from src.actionable_dates import ActionableSuggestion


def build_actionable_alert_message(suggestion: ActionableSuggestion) -> str:
    header = "Cleanup Agent Reminder" if not suggestion.date_is_certain else "Cleanup Agent Alert"
    # %-d (no leading zero) isn't portable across platforms (fails on
    # Windows) - %d always works, just zero-pads single-digit days.
    date_str = suggestion.suggested_date.strftime("%B %d, %Y")
    return f"{header}\n\n{suggestion.summary}\n\nDate: {date_str}"


def build_security_alert_message(sender: str, detailed_type: str) -> str:
    return (
        "Cleanup Agent Security Alert\n\n"
        "An important account security email was detected.\n\n"
        f"Sender: {sender}\n"
        f"Type: {detailed_type or 'Security Alert'}"
    )


def build_daily_summary_message(scanned: int, counts: dict) -> str:
    lines = ["Cleanup Agent Daily Summary", "", f"Emails scanned: {scanned}"]
    for label, n in counts.items():
        lines.append(f"{label}: {n}")
    return "\n".join(lines)
