from tests.conftest import make_email

from src.config import Category
from src.rule_engine import classify_by_rules


def test_known_otp_sender_detected():
    email = make_email(sender="noreply@accounts.google.com", subject="Your verification code")
    result = classify_by_rules(email)
    assert result is not None
    assert result.category == Category.OTP_VERIFICATION
    assert result.source == "rule"


def test_newsletter_domain_detected():
    email = make_email(sender="hello@substack.com", subject="This week's issue")
    result = classify_by_rules(email)
    assert result.category == Category.NEWSLETTER


def test_invoice_subject_detected():
    email = make_email(sender="billing@vendor.com", subject="Your invoice #4471", unsubscribe=False)
    result = classify_by_rules(email)
    assert result.category == Category.INVOICE_RECEIPT


def test_unsubscribe_header_without_other_match_is_promotional():
    email = make_email(sender="deals@randomshop.com", subject="Check out our catalog", unsubscribe=True)
    result = classify_by_rules(email)
    assert result.category == Category.PROMOTIONAL


def test_no_rule_match_returns_none():
    email = make_email(sender="friend@personalmail.com", subject="Dinner Friday?", unsubscribe=False)
    result = classify_by_rules(email)
    assert result is None
