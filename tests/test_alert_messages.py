from datetime import date

from src.actionable_dates import ActionableSuggestion
from src.alert_messages import (
    build_actionable_alert_message,
    build_daily_summary_message,
    build_security_alert_message,
)
# Conciseness bound for the assertions below - not tied to the actual
# Telegram limit (4096, see telegram_service.MAX_ALERT_LENGTH); this just
# confirms alerts stay short, not a wall of text.
MAX_ALERT_LENGTH = 1024


def test_bill_alert_is_concise_and_structured():
    suggestion = ActionableSuggestion(
        message_id="m1",
        summary="Electricity Board <billing@eb.gov.in>: Electricity Bill Rs.2450",
        suggested_date=date(2026, 9, 5),
        date_is_certain=True,
    )
    msg = build_actionable_alert_message(suggestion)
    assert "Cleanup Agent" in msg
    assert "September 05, 2026" in msg
    assert len(msg) < MAX_ALERT_LENGTH
    assert msg.count("\n") < 10  # concise, not a wall of text


def test_uncertain_date_alert_uses_reminder_header():
    suggestion = ActionableSuggestion(
        message_id="m2", summary="Some Sender: Payment due soon",
        suggested_date=date(2026, 9, 8), date_is_certain=False,
    )
    msg = build_actionable_alert_message(suggestion)
    assert "Reminder" in msg


def test_security_alert_is_concise_and_never_includes_raw_body():
    email_body = "This is the full private email body that must never be forwarded verbatim."
    msg = build_security_alert_message(sender="no-reply@accounts.google.com", detailed_type="Login Alert")
    assert email_body not in msg
    assert "Cleanup Agent Security Alert" in msg
    assert "no-reply@accounts.google.com" in msg
    assert "Login Alert" in msg
    assert len(msg) < MAX_ALERT_LENGTH


def test_daily_summary_message_structure():
    msg = build_daily_summary_message(126, {"Promotions": 47, "Newsletters": 18, "Important": 6})
    assert "Emails scanned: 126" in msg
    assert "Promotions: 47" in msg
    assert len(msg) < MAX_ALERT_LENGTH
