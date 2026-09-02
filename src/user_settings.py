"""Per-user Telegram destination and notification preferences.

Backed by Upstash Redis (REST API) - a small managed key-value store, a
good fit for this data shape (user_id -> one JSON blob, no relational
structure needed) and durable across Streamlit Community Cloud reboots/
redeploys, unlike the local-disk JSON this replaced.

This is the single source of truth `telegram_service.send_owner_alert`
reads before every send - never trust a chat ID from anywhere else (UI
form input only ever writes here via `save_telegram_settings`; nothing
else is allowed to write it, and nothing downstream accepts a chat ID as
an argument at all). Never stores the bot token or any Composio secret -
those stay in env vars/Streamlit secrets, never touch this store.

Fails LOUD, not quiet: if Redis is unreachable, load/save raise
TelegramSettingsStoreError rather than silently returning/pretending
success with default (empty) settings. The only way to get back an empty
TelegramSettings() is a genuine, successful "no key found" answer from
Redis - a real answer, not a guess. A network failure never gets
conflated with "this user has no settings"; telegram_service.py treats
a store error as a hard stop before ever resolving a send destination.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from src.config import AppConfig
from src.logger import get_logger
from src.redis_store import RedisStoreError, UpstashRedisStore

logger = get_logger(__name__)

# Notification categories a user can opt into independently. "daily_summary"
# is handled the same way even though it isn't a mail category - one flat
# namespace keeps the enable-check in telegram_service uniform.
ALERT_CATEGORIES = {
    "bills_payments": "Bills & Payments",
    "security_alerts": "Security Alerts",
    "deadlines": "Deadlines (Action Center)",
    "subscription_renewals": "Subscription Renewals",
    "interviews_appointments": "Interviews / Appointments",
    "important_emails": "Important Emails",
    "daily_summary": "Daily Cleanup Summary",
}

_KEY_PREFIX = "cleanup-agent:telegram-settings:"


class TelegramSettingsStoreError(Exception):
    """Redis unreachable, timed out, or returned malformed data. Callers
    must treat this as "cannot verify" and refuse to act, never as
    "no settings found, safe to proceed with defaults"."""


@dataclass
class TelegramSettings:
    enabled: bool = False
    chat_id: str = ""  # Telegram numeric chat ID, resolved via the verified connect flow
    display_label: str = ""  # e.g. "@username" - cosmetic only, never used to send
    categories: dict = field(default_factory=lambda: {k: False for k in ALERT_CATEGORIES})

    def category_enabled(self, category: str) -> bool:
        return bool(self.enabled and self.chat_id and self.categories.get(category, False))


def _store(config: AppConfig) -> UpstashRedisStore:
    if not (config.upstash_redis_url and config.upstash_redis_token):
        raise TelegramSettingsStoreError("Upstash Redis is not configured for this deployment.")
    return UpstashRedisStore(config.upstash_redis_url, config.upstash_redis_token)


def load_telegram_settings(config: AppConfig, user_id: str) -> TelegramSettings:
    store = _store(config)
    try:
        raw = store.get(_KEY_PREFIX + user_id)
    except RedisStoreError as exc:
        raise TelegramSettingsStoreError(f"Could not load Telegram settings: {exc}") from exc
    if not raw:
        return TelegramSettings()
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise TelegramSettingsStoreError(f"Corrupt Telegram settings data: {exc}") from exc
    categories = {k: bool(data.get("categories", {}).get(k, False)) for k in ALERT_CATEGORIES}
    return TelegramSettings(
        enabled=bool(data.get("enabled", False)),
        chat_id=str(data.get("chat_id", "")),
        display_label=str(data.get("display_label", "")),
        categories=categories,
    )


def save_telegram_settings(config: AppConfig, user_id: str, settings: TelegramSettings) -> None:
    store = _store(config)
    try:
        store.set(_KEY_PREFIX + user_id, json.dumps(asdict(settings)))
    except RedisStoreError as exc:
        raise TelegramSettingsStoreError(f"Could not save Telegram settings: {exc}") from exc
