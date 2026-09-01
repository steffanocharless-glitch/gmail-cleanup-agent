"""Per-user Telegram destination and notification preferences.

This is the single source of truth `telegram_service.send_owner_alert`
reads before every send - never trust a chat ID from anywhere else (UI
form input only ever writes here via `save_telegram_settings`; nothing
else is allowed to write it, and nothing downstream accepts a chat ID as
an argument at all).

Storage abstraction: callers only ever see `load_telegram_settings` /
`save_telegram_settings` / `TelegramSettings` - the JSON-file backing is
an implementation detail confined to this module. Swapping it for a real
database later (see the durability caveat below) means rewriting these
two functions, not touching any caller.

Stored one JSON file per user_id under USER_SETTINGS_DIR. Same durability
caveat as the classification cache: local disk, not guaranteed to survive
a Streamlit Community Cloud reboot/redeploy - this is DEV-GRADE storage,
not production-safe for data that must persist. See config.py.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from src.config import USER_SETTINGS_DIR
from src.logger import get_logger

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


@dataclass
class TelegramSettings:
    enabled: bool = False
    chat_id: str = ""  # Telegram numeric chat ID, resolved via the connect flow
    display_label: str = ""  # e.g. "@username" - cosmetic only, never used to send
    categories: dict = field(default_factory=lambda: {k: False for k in ALERT_CATEGORIES})

    def category_enabled(self, category: str) -> bool:
        return bool(self.enabled and self.chat_id and self.categories.get(category, False))


def _settings_path(user_id: str) -> Path:
    # user_id is already a sha256 hex digest (see identity.py) - always
    # filesystem-safe, no sanitization needed.
    return USER_SETTINGS_DIR / f"{user_id}_telegram.json"


def load_telegram_settings(user_id: str) -> TelegramSettings:
    path = _settings_path(user_id)
    if not path.exists():
        return TelegramSettings()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read Telegram settings for user, using defaults: %s", exc)
        return TelegramSettings()
    categories = {k: bool(raw.get("categories", {}).get(k, False)) for k in ALERT_CATEGORIES}
    return TelegramSettings(
        enabled=bool(raw.get("enabled", False)),
        chat_id=str(raw.get("chat_id", "")),
        display_label=str(raw.get("display_label", "")),
        categories=categories,
    )


def save_telegram_settings(user_id: str, settings: TelegramSettings) -> None:
    USER_SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    _settings_path(user_id).write_text(
        json.dumps(asdict(settings)), encoding="utf-8"
    )
