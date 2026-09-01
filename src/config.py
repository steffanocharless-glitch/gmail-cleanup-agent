"""Centralized configuration for the Gmail Cleanup Agent.

All tunable thresholds, age rules, and category->action defaults live here
so nothing is hard-coded elsewhere in the application.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Plain stdlib logger, not src.logger.get_logger - that module imports from
# this one (CLEANUP_LOG_DIR), so using it here would be a circular import.
_logger = logging.getLogger(__name__)
_secrets_warning_shown = False

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CLEANUP_LOG_DIR = PROJECT_ROOT / "data" / "cleanup_logs"
CACHE_DIR = PROJECT_ROOT / "data" / "cache"
# Per-user Telegram destination/preferences. Local filesystem, same pattern
# as CACHE_DIR/CLEANUP_LOG_DIR - on Streamlit Community Cloud this is NOT
# durable across app reboots/redeploys (ephemeral disk). Dev-grade storage;
# a deployment that needs this to survive reboots needs a real database -
# see user_settings.py's docstring for the swap point.
USER_SETTINGS_DIR = PROJECT_ROOT / "data" / "user_settings"


class Category:
    """Level-1 categories: what an email's PURPOSE is, never who sent it.
    A single sender (a bank, Amazon, GitHub...) routinely lands mail in many
    of these - see the `context` field on ClassificationResult for the
    sender/domain side of the hierarchy."""
    TRANSACTIONS = "Transactions"
    OTP_VERIFICATION = "OTP & Verification"
    SECURITY_ALERTS = "Security Alerts"
    STATEMENTS_DOCUMENTS = "Statements & Documents"
    BILLS_PAYMENTS = "Bills & Payments"
    ORDERS_PURCHASES = "Orders & Purchases"
    SHIPPING_DELIVERY = "Shipping & Delivery"
    ACCOUNT_SERVICE_UPDATES = "Account & Service Updates"
    SUBSCRIPTIONS = "Subscriptions"
    WORK = "Work"
    PERSONAL = "Personal"
    TRAVEL = "Travel"
    APP_SYSTEM_NOTIFICATIONS = "App / System Notifications"
    PROMOTIONS_MARKETING = "Promotions & Marketing"
    NEWSLETTERS = "Newsletters"
    SOCIAL = "Social"
    FINANCE_INVESTMENT = "Finance / Investment"
    SPAM_SUSPICIOUS = "Spam / Suspicious"
    UNCERTAIN = "Uncertain"
    OTHER = "Other"

    ALL = [
        TRANSACTIONS, OTP_VERIFICATION, SECURITY_ALERTS, STATEMENTS_DOCUMENTS,
        BILLS_PAYMENTS, ORDERS_PURCHASES, SHIPPING_DELIVERY,
        ACCOUNT_SERVICE_UPDATES, SUBSCRIPTIONS, WORK, PERSONAL, TRAVEL,
        APP_SYSTEM_NOTIFICATIONS, PROMOTIONS_MARKETING, NEWSLETTERS, SOCIAL,
        FINANCE_INVESTMENT, SPAM_SUSPICIOUS, UNCERTAIN, OTHER,
    ]


class Action:
    KEEP = "KEEP"
    ARCHIVE = "ARCHIVE"
    TRASH = "TRASH"
    ADD_LABEL = "ADD_LABEL"
    REVIEW = "REVIEW"


# Categories that must never be auto-trashed regardless of confidence/age.
# OTP_VERIFICATION is deliberately excluded here - it is instead protected
# only within a recent time window (see safety_rules.RECENT_SECURITY_WINDOW_HOURS),
# so stale OTPs can still be cleaned up while fresh ones can't.
PROTECTED_CATEGORIES = {
    Category.TRANSACTIONS,
    Category.SECURITY_ALERTS,
    Category.STATEMENTS_DOCUMENTS,
    Category.BILLS_PAYMENTS,
    Category.ORDERS_PURCHASES,
    Category.ACCOUNT_SERVICE_UPDATES,
    Category.WORK,
    Category.PERSONAL,
    Category.TRAVEL,
    Category.FINANCE_INVESTMENT,
    Category.UNCERTAIN,
}

# Level-1 categories a message may enter the cleanup workflow for (see
# `cleanup_safe` on ClassificationResult). This is informational only - it
# never bypasses safety_rules.decide_action or the trash confirmation step.
CLEANUP_SAFE_CATEGORIES = {
    Category.PROMOTIONS_MARKETING,
    Category.NEWSLETTERS,
    Category.SOCIAL,
    Category.SPAM_SUSPICIOUS,
    Category.APP_SYSTEM_NOTIFICATIONS,
    Category.SHIPPING_DELIVERY,
    Category.SUBSCRIPTIONS,
}


def compute_cleanup_safe(category: str, is_promotional: bool, is_security_sensitive: bool) -> bool:
    """A message may enter the cleanup workflow if it's promotional or in a
    naturally low-stakes category, and never if it's security-sensitive."""
    if is_security_sensitive:
        return False
    return is_promotional or category in CLEANUP_SAFE_CATEGORIES

# Sender/subject keyword hints that always force protection regardless of
# classifier output (belt-and-suspenders on top of category protection).
PROTECTED_KEYWORDS = [
    "password reset", "security alert", "account recovery", "verify your identity",
    "tax", "irs", "invoice", "receipt", "payment", "bank", "statement",
    "government", "legal", "hr", "human resources", "payroll", "w-2", "w2",
    "unauthorized sign-in", "suspicious activity", "2-step verification",
]

# Default recommended operation per category before confidence/age rules apply.
DEFAULT_CATEGORY_ACTION = {
    Category.TRANSACTIONS: Action.KEEP,
    Category.OTP_VERIFICATION: Action.ARCHIVE,
    Category.SECURITY_ALERTS: Action.KEEP,
    Category.STATEMENTS_DOCUMENTS: Action.KEEP,
    Category.BILLS_PAYMENTS: Action.KEEP,
    Category.ORDERS_PURCHASES: Action.KEEP,
    Category.SHIPPING_DELIVERY: Action.ARCHIVE,
    Category.ACCOUNT_SERVICE_UPDATES: Action.KEEP,
    Category.SUBSCRIPTIONS: Action.ARCHIVE,
    Category.WORK: Action.KEEP,
    Category.PERSONAL: Action.KEEP,
    Category.TRAVEL: Action.KEEP,
    Category.APP_SYSTEM_NOTIFICATIONS: Action.ARCHIVE,
    Category.PROMOTIONS_MARKETING: Action.ARCHIVE,
    Category.NEWSLETTERS: Action.ARCHIVE,
    Category.SOCIAL: Action.ARCHIVE,
    Category.FINANCE_INVESTMENT: Action.KEEP,
    Category.SPAM_SUSPICIOUS: Action.TRASH,
    Category.UNCERTAIN: Action.REVIEW,
    Category.OTHER: Action.REVIEW,
}

# Categories eligible for age-based cleanup rules, and the default minimum
# age (in days) a message must reach before the rule applies.
DEFAULT_AGE_RULES_DAYS = {
    Category.OTP_VERIFICATION: 30,
    Category.SHIPPING_DELIVERY: 14,
    Category.SUBSCRIPTIONS: 30,
    Category.APP_SYSTEM_NOTIFICATIONS: 45,
    Category.PROMOTIONS_MARKETING: 21,
    Category.NEWSLETTERS: 30,
    Category.SOCIAL: 30,
    Category.SPAM_SUSPICIOUS: 7,
}


def _raw_env(name: str) -> str | None:
    """Read a config value with Streamlit Community Cloud's st.secrets taking
    priority (the canonical source when deployed), falling back to the OS
    environment (.env via python-dotenv, for local dev) when no secrets.toml
    exists or the key isn't set there."""
    global _secrets_warning_shown
    try:
        import streamlit as st
        from streamlit.errors import StreamlitSecretNotFoundError

        try:
            if name in st.secrets:
                return str(st.secrets[name])
        except StreamlitSecretNotFoundError as exc:
            # Streamlit raises this SAME exception class both when no
            # secrets.toml exists at all (expected, silent - local dev) and
            # when one exists but fails to parse (a real bug that must not
            # be swallowed silently, or it's indistinguishable from "secret
            # not set"). Only the message text tells them apart.
            if "no secrets found" not in str(exc).lower() and not _secrets_warning_shown:
                _logger.warning("st.secrets failed to load (falling back to env vars): %s", exc)
                _secrets_warning_shown = True
    except ImportError:
        pass  # streamlit not installed in this context (e.g. some tooling)
    return os.getenv(name)


def _env_str(name: str, default: str = "") -> str:
    val = _raw_env(name)
    return val if val else default


def _env_float(name: str, default: float) -> float:
    raw = _raw_env(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = _raw_env(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = _raw_env(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class ConfidenceThresholds:
    """Confidence bands that gate automated action.

    >= high: recommendation may be auto-applied on user confirmation.
    between low and high: manual review required.
    < low: KEEP automatically, never touched.
    """
    high: float = field(default_factory=lambda: _env_float("CONFIDENCE_HIGH", 0.90))
    low: float = field(default_factory=lambda: _env_float("CONFIDENCE_LOW", 0.70))

    def band(self, confidence: float) -> str:
        if confidence >= self.high:
            return "high"
        if confidence >= self.low:
            return "manual_review"
        return "auto_keep"


@dataclass
class AppConfig:
    # Composio
    composio_api_key: str = field(default_factory=lambda: _env_str("COMPOSIO_API_KEY"))
    composio_gmail_auth_config_id: str = field(
        default_factory=lambda: _env_str("COMPOSIO_GMAIL_AUTH_CONFIG_ID")
    )
    composio_callback_url: str = field(
        default_factory=lambda: _env_str("COMPOSIO_CALLBACK_URL")
    )
    # Optional: Google Calendar auth config for actionable-date suggestions
    # (bills due, renewals, interviews...). A separate Composio connection
    # from Gmail even though it's the same Google account - Composio scopes
    # OAuth per toolkit. Feature is hidden in the UI when this is blank.
    composio_calendar_auth_config_id: str = field(
        default_factory=lambda: _env_str("COMPOSIO_CALENDAR_AUTH_CONFIG_ID")
    )
    # Optional: Telegram alert channel. Telegram bots authenticate via a bot
    # token (API_KEY scheme), not OAuth - there is no per-user redirect flow.
    # The bot is one shared app-level connection (created once from
    # telegram_bot_token); what's per-user is the destination chat_id,
    # stored in user_settings.py, resolved by the user picking their own
    # chat out of recent bot messages ("Detect My Telegram Chat" in the UI).
    # Feature hidden when either value is blank.
    composio_telegram_auth_config_id: str = field(
        default_factory=lambda: _env_str("COMPOSIO_TELEGRAM_AUTH_CONFIG_ID")
    )
    telegram_bot_token: str = field(
        default_factory=lambda: _env_str("TELEGRAM_BOT_TOKEN")
    )

    # Gemini
    gemini_api_key: str = field(default_factory=lambda: _env_str("GEMINI_API_KEY"))
    gemini_model: str = field(
        default_factory=lambda: _env_str("GEMINI_MODEL", "gemini-3.6-flash")
    )

    # Batching / rate limits
    fetch_batch_size: int = field(default_factory=lambda: _env_int("FETCH_BATCH_SIZE", 100))
    classify_batch_size: int = field(default_factory=lambda: _env_int("CLASSIFY_BATCH_SIZE", 100))
    max_messages_per_scan: int = field(
        default_factory=lambda: _env_int("MAX_MESSAGES_PER_SCAN", 2000)
    )
    gmail_retry_attempts: int = field(default_factory=lambda: _env_int("GMAIL_RETRY_ATTEMPTS", 3))
    gmail_retry_backoff_seconds: float = field(
        default_factory=lambda: _env_float("GMAIL_RETRY_BACKOFF_SECONDS", 2.0)
    )

    # Safety
    dry_run: bool = field(default_factory=lambda: _env_bool("DRY_RUN_DEFAULT", True))
    thresholds: ConfidenceThresholds = field(default_factory=ConfidenceThresholds)
    age_rules_days: dict = field(default_factory=lambda: dict(DEFAULT_AGE_RULES_DAYS))
    category_actions: dict = field(default_factory=lambda: dict(DEFAULT_CATEGORY_ACTION))
    protected_categories: set = field(default_factory=lambda: set(PROTECTED_CATEGORIES))

    def validate(self) -> list[str]:
        problems = []
        if not self.composio_api_key:
            problems.append("COMPOSIO_API_KEY is not set")
        if not self.composio_gmail_auth_config_id:
            problems.append("COMPOSIO_GMAIL_AUTH_CONFIG_ID is not set")
        if not self.gemini_api_key:
            problems.append("GEMINI_API_KEY is not set")
        return problems


def get_config() -> AppConfig:
    CLEANUP_LOG_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    USER_SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    return AppConfig()
