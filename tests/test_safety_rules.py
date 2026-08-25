from tests.conftest import make_email

from src.config import Action, Category
from src.models import ClassificationResult
from src.safety_rules import decide_action, requires_trash_confirmation


def classify(category, confidence, reason="test"):
    return ClassificationResult(
        message_id="msg-1", category=category, confidence=confidence,
        reason=reason, source="rule",
    )


def test_finance_email_never_trashed_even_high_confidence(config):
    email = make_email(subject="Your bank statement is ready")
    result = decide_action(email, classify(Category.FINANCE_INVESTMENT, 0.99), config)
    assert result.recommended_action == Action.KEEP
    assert result.protected is True


def test_important_email_protected(config):
    email = make_email(subject="Password reset requested", days_old=0)
    result = decide_action(email, classify(Category.SECURITY_ALERTS, 0.95), config)
    assert result.recommended_action == Action.KEEP
    assert result.protected is True


def test_low_confidence_never_deleted(config):
    email = make_email(subject="Some ambiguous mail")
    result = decide_action(email, classify(Category.SPAM_SUSPICIOUS, 0.55), config)
    assert result.recommended_action == Action.KEEP


def test_uncertain_category_defaults_to_keep(config):
    email = make_email()
    result = decide_action(email, classify(Category.UNCERTAIN, 0.99), config)
    assert result.recommended_action == Action.KEEP
    assert result.protected is True


def test_high_confidence_promotional_past_age_rule_is_archived(config):
    email = make_email(days_old=45, unsubscribe=True)
    result = decide_action(email, classify(Category.PROMOTIONS_MARKETING, 0.95), config)
    assert result.recommended_action == Action.ARCHIVE


def test_promotional_under_age_threshold_kept(config):
    email = make_email(days_old=5, unsubscribe=True)
    result = decide_action(email, classify(Category.PROMOTIONS_MARKETING, 0.95), config)
    assert result.recommended_action == Action.KEEP


def test_manual_review_band_for_mid_confidence(config):
    email = make_email(days_old=45)
    result = decide_action(email, classify(Category.PROMOTIONS_MARKETING, 0.80), config)
    assert result.recommended_action == Action.REVIEW


def test_recent_security_email_protected_even_if_flagged_otp(config):
    email = make_email(subject="Your one-time verification code", days_old=0)
    result = decide_action(email, classify(Category.OTP_VERIFICATION, 0.97), config)
    assert result.recommended_action == Action.KEEP
    assert result.protected is True


def test_trash_requires_confirmation_flag():
    assert requires_trash_confirmation(Action.TRASH) is True
    assert requires_trash_confirmation(Action.ARCHIVE) is False


def test_same_bank_sender_transaction_kept_promotion_cleanup_candidate(config):
    """IDFC transaction alert -> Keep; IDFC personal loan promotion -> archive-eligible."""
    txn_email = make_email(sender="alerts@idfcfirstbank.com", subject="Rs.5,000 debited from your account")
    txn_result = decide_action(
        txn_email,
        ClassificationResult(
            message_id="msg-txn", category=Category.TRANSACTIONS, confidence=0.95,
            reason="test", source="rule", context="Banking", is_promotional=False,
        ),
        config,
    )
    assert txn_result.recommended_action == Action.KEEP
    assert txn_result.protected is True

    promo_email = make_email(
        sender="alerts@idfcfirstbank.com", subject="Personal loan offer - apply now", days_old=45,
    )
    promo_result = decide_action(
        promo_email,
        ClassificationResult(
            message_id="msg-promo", category=Category.PROMOTIONS_MARKETING, confidence=0.95,
            reason="test", source="rule", context="Banking", is_promotional=True,
        ),
        config,
    )
    assert promo_result.recommended_action == Action.ARCHIVE
    assert promo_result.protected is False
