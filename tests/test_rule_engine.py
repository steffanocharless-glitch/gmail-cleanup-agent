from tests.conftest import make_email

from src.config import Category
from src.rule_engine import classify_by_rules, derive_context


def _classify(sender, subject, snippet=""):
    email = make_email(sender=sender, subject=subject, snippet=snippet, unsubscribe=False)
    return classify_by_rules(email)


# ---------------------------------------------------------------------------
# Banking - same sender, different purposes.
# ---------------------------------------------------------------------------

BANK_SENDER = "alerts@idfcfirstbank.com"


def test_bank_transaction_alert():
    result = _classify(BANK_SENDER, "Rs.5,000 debited from your account")
    assert result.category == Category.TRANSACTIONS
    assert result.context == "Banking"
    assert result.detailed_type == "Debit Alert"
    assert result.is_promotional is False


def test_bank_otp():
    result = _classify(BANK_SENDER, "Your OTP is 123456")
    assert result.category == Category.OTP_VERIFICATION
    assert result.context == "Banking"
    assert result.is_security_sensitive is True


def test_bank_statement():
    result = _classify(BANK_SENDER, "Your monthly account statement is ready")
    assert result.category == Category.STATEMENTS_DOCUMENTS
    assert result.context == "Banking"


def test_bank_security_alert():
    result = _classify(BANK_SENDER, "Unusual login detected on your account")
    assert result.category == Category.SECURITY_ALERTS
    assert result.context == "Banking"
    assert result.is_security_sensitive is True


def test_bank_kyc_notice():
    result = _classify(BANK_SENDER, "Complete your KYC to continue using your account")
    assert result.category == Category.ACCOUNT_SERVICE_UPDATES
    assert result.detailed_type == "KYC"
    assert result.context == "Banking"


def test_bank_loan_promotion():
    result = _classify(BANK_SENDER, "Get a personal loan at 10.5% - apply now")
    assert result.category == Category.PROMOTIONS_MARKETING
    assert result.detailed_type == "Loan Promotion"
    assert result.is_promotional is True
    assert result.context == "Banking"


def test_bank_credit_card_promotion():
    result = _classify(BANK_SENDER, "Exclusive offer: get a credit card with cashback offer")
    assert result.category == Category.PROMOTIONS_MARKETING
    assert result.detailed_type == "Credit Card Promotion"
    assert result.is_promotional is True


def test_bank_transaction_never_flagged_promotional():
    """Promo from the same bank must not taint a real transaction alert."""
    result = _classify(BANK_SENDER, "Your payment of Rs.5,000 was successful")
    assert result.category == Category.TRANSACTIONS
    assert result.is_promotional is False


def test_bank_preapproved_money_language_is_promotional_not_transaction():
    result = _classify(BANK_SENDER, "Your Rs.50,000 pre-approved amount is waiting")
    assert result.category == Category.PROMOTIONS_MARKETING
    assert result.is_promotional is True


# ---------------------------------------------------------------------------
# Amazon - same sender, different purposes.
# ---------------------------------------------------------------------------

AMAZON_SENDER = "order-update@amazon.in"


def test_amazon_order_confirmation():
    result = _classify(AMAZON_SENDER, "Your order confirmation #123-4567890")
    assert result.category == Category.ORDERS_PURCHASES
    assert result.context == "Amazon"


def test_amazon_delivery_notification():
    result = _classify(AMAZON_SENDER, "Your package has been delivered")
    assert result.category == Category.SHIPPING_DELIVERY
    assert result.context == "Amazon"


def test_amazon_promotion():
    result = _classify(AMAZON_SENDER, "Limited time offer - shop now and save up to 40%")
    assert result.category == Category.PROMOTIONS_MARKETING
    assert result.is_promotional is True
    assert result.context == "Amazon"


# ---------------------------------------------------------------------------
# GitHub - same sender, different purposes.
# ---------------------------------------------------------------------------

GITHUB_SENDER = "notifications@github.com"


def test_github_security_notification():
    result = _classify(GITHUB_SENDER, "A new OAuth application was added to your account")
    assert result.category == Category.SECURITY_ALERTS
    assert result.context == "GitHub"
    assert result.is_security_sensitive is True


def test_github_repository_notification():
    result = _classify(GITHUB_SENDER, "Pull request #123 was merged")
    assert result.category == Category.APP_SYSTEM_NOTIFICATIONS
    assert result.context == "GitHub"


def test_github_promotional_email():
    result = _classify(GITHUB_SENDER, "Exclusive offer: upgrade now to GitHub Copilot")
    assert result.category == Category.PROMOTIONS_MARKETING
    assert result.is_promotional is True
    assert result.context == "GitHub"


# ---------------------------------------------------------------------------
# Cross-sender: same sender lands in different primary categories.
# ---------------------------------------------------------------------------

def test_same_bank_sender_spans_multiple_primary_categories():
    categories = {
        _classify(BANK_SENDER, "Rs.5,000 debited from your account").category,
        _classify(BANK_SENDER, "Your OTP is 123456").category,
        _classify(BANK_SENDER, "Your monthly account statement is ready").category,
        _classify(BANK_SENDER, "Get a personal loan at 10.5% - apply now").category,
        _classify(BANK_SENDER, "Unusual login detected on your account").category,
    }
    assert len(categories) >= 4


def test_same_amazon_sender_spans_multiple_primary_categories():
    categories = {
        _classify(AMAZON_SENDER, "Your order confirmation #123").category,
        _classify(AMAZON_SENDER, "Your package has been delivered").category,
        _classify(AMAZON_SENDER, "Limited time offer - shop now").category,
    }
    assert len(categories) == 3


# ---------------------------------------------------------------------------
# Misc / preserved behavior.
# ---------------------------------------------------------------------------

def test_unsubscribe_header_without_other_match_is_promotional():
    email = make_email(sender="deals@randomshop.com", subject="Check out our catalog", unsubscribe=True)
    result = classify_by_rules(email)
    assert result.category == Category.PROMOTIONS_MARKETING
    assert result.is_promotional is True


def test_no_rule_match_returns_none():
    email = make_email(
        sender="friend@personalmail.com", subject="Dinner Friday?", snippet="See you at 7?",
        unsubscribe=False,
    )
    result = classify_by_rules(email)
    assert result is None


def test_derive_context_falls_back_to_domain_label():
    assert derive_context("hello@acme-corp.io") == "Acme Corp"


def test_derive_context_unknown_sender():
    assert derive_context("not-an-email") == "Unknown"
