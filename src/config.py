"""Centralized configuration for the Gmail Cleanup Agent.

All tunable thresholds, age rules, and category->action defaults live here
so nothing is hard-coded elsewhere in the application.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CLEANUP_LOG_DIR = PROJECT_ROOT / "data" / "cleanup_logs"
CACHE_DIR = PROJECT_ROOT / "data" / "cache"


class Category:
    IMPORTANT = "Important"
    REQUIRES_ACTION = "Requires Action"
    WORK = "Work"
    CLIENT = "Client"
    FINANCE = "Finance"
    INVOICE_RECEIPT = "Invoice / Receipt"
    PERSONAL = "Personal"
    NEWSLETTER = "Newsletter"
    PROMOTIONAL = "Promotional / Marketing"
    SOCIAL = "Social Media Notification"
    AUTOMATED_NOTIFICATION = "Automated Notification"
    OTP_VERIFICATION = "OTP / Verification"
    SPAM = "Spam / Useless"
    UNCERTAIN = "Uncertain"

    ALL = [
        IMPORTANT, REQUIRES_ACTION, WORK, CLIENT, FINANCE, INVOICE_RECEIPT,
        PERSONAL, NEWSLETTER, PROMOTIONAL, SOCIAL, AUTOMATED_NOTIFICATION,
        OTP_VERIFICATION, SPAM, UNCERTAIN,
    ]


class Action:
    KEEP = "KEEP"
    ARCHIVE = "ARCHIVE"
    TRASH = "TRASH"
    ADD_LABEL = "ADD_LABEL"
    REVIEW = "REVIEW"


# Categories that must never be auto-trashed regardless of confidence/age.
PROTECTED_CATEGORIES = {
    Category.IMPORTANT,
    Category.REQUIRES_ACTION,
    Category.FINANCE,
    Category.INVOICE_RECEIPT,
    Category.CLIENT,
    Category.PERSONAL,
    Category.UNCERTAIN,
}

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
    Category.IMPORTANT: Action.KEEP,
    Category.REQUIRES_ACTION: Action.KEEP,
    Category.WORK: Action.KEEP,
    Category.CLIENT: Action.KEEP,
    Category.FINANCE: Action.KEEP,
    Category.INVOICE_RECEIPT: Action.KEEP,
    Category.PERSONAL: Action.KEEP,
    Category.NEWSLETTER: Action.ARCHIVE,
    Category.PROMOTIONAL: Action.ARCHIVE,
    Category.SOCIAL: Action.ARCHIVE,
    Category.AUTOMATED_NOTIFICATION: Action.ARCHIVE,
    Category.OTP_VERIFICATION: Action.ARCHIVE,
    Category.SPAM: Action.TRASH,
    Category.UNCERTAIN: Action.REVIEW,
}

# Categories eligible for age-based cleanup rules, and the default minimum
# age (in days) a message must reach before the rule applies.
DEFAULT_AGE_RULES_DAYS = {
    Category.PROMOTIONAL: 30,
    Category.NEWSLETTER: 30,
    Category.SOCIAL: 30,
    Category.OTP_VERIFICATION: 30,
    Category.AUTOMATED_NOTIFICATION: 45,
    Category.SPAM: 7,
}


def _raw_env(name: str) -> str | None:
    """Read a config value with Streamlit Community Cloud's st.secrets taking
    priority (the canonical source when deployed), falling back to the OS
    environment (.env via python-dotenv, for local dev) when no secrets.toml
    exists or the key isn't set there."""
    try:
        import streamlit as st
        if name in st.secrets:
            return str(st.secrets[name])
    except Exception:  # noqa: BLE001 - no secrets.toml / not running under Streamlit
        pass
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

    # Gemini
    gemini_api_key: str = field(default_factory=lambda: _env_str("GEMINI_API_KEY"))
    gemini_model: str = field(
        default_factory=lambda: _env_str("GEMINI_MODEL", "gemini-3.6-flash")
    )

    # Batching / rate limits
    fetch_batch_size: int = field(default_factory=lambda: _env_int("FETCH_BATCH_SIZE", 100))
    classify_batch_size: int = field(default_factory=lambda: _env_int("CLASSIFY_BATCH_SIZE", 20))
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
    return AppConfig()
