from datetime import date, datetime, timedelta, timezone

from tests.conftest import make_email

from src.actionable_dates import detect_actionable_date


def test_bill_due_date_detected_certain():
    email = make_email(
        subject="Electricity Bill", snippet="Amount Rs.2450 due date is 03/09/2026",
        days_old=1, unsubscribe=False,
    )
    result = detect_actionable_date(email)
    assert result is not None
    assert result.date_is_certain is True
    assert result.suggested_date == date(2026, 9, 3)


def test_subscription_renewal_textual_date():
    email = make_email(
        subject="Your plan renews on September 3, 2026", snippet="",
        days_old=1, unsubscribe=False,
    )
    result = detect_actionable_date(email)
    assert result is not None
    assert result.date_is_certain is True
    assert result.suggested_date == date(2026, 9, 3)


def test_no_trigger_phrase_returns_none():
    email = make_email(
        subject="Your order confirmation #123", snippet="Arriving 03/09/2026",
        days_old=1, unsubscribe=False,
    )
    assert detect_actionable_date(email) is None


def test_promotional_date_without_actionable_phrase_ignored():
    email = make_email(
        subject="Sale ends 25/08/2026", snippet="Limited time offer",
        days_old=1, unsubscribe=True,
    )
    assert detect_actionable_date(email) is None


def test_trigger_phrase_without_parseable_date_is_uncertain():
    email = make_email(
        subject="Payment due soon", snippet="Please pay at your earliest convenience",
        days_old=1, unsubscribe=False,
    )
    result = detect_actionable_date(email)
    assert result is not None
    assert result.date_is_certain is False
    assert result.suggested_date == email.received_at.date() + timedelta(days=7)


def test_yearless_date_in_past_bumped_to_next_occurrence():
    received = datetime(2026, 11, 1, tzinfo=timezone.utc)
    email = make_email(
        subject="Interview scheduled on 5 Jan", snippet="",
        days_old=0, unsubscribe=False,
    )
    email.received_at = received
    result = detect_actionable_date(email)
    assert result is not None
    assert result.suggested_date == date(2027, 1, 5)


def test_appointment_detected():
    email = make_email(
        subject="Appointment scheduled", snippet="Your appointment is on 15 Sep 2026",
        days_old=1, unsubscribe=False,
    )
    result = detect_actionable_date(email)
    assert result is not None
    assert result.suggested_date == date(2026, 9, 15)
