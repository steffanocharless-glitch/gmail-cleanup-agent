"""Telegram alert channel built on top of ComposioService.

Telegram bots authenticate via a bot token (API_KEY scheme, from
@BotFather) - there is no per-Streamlit-user OAuth redirect flow like
Gmail/Calendar have. The bot connection is ONE shared app-level resource
(BOT_USER_ID is a fixed internal identity, not any real signed-in user),
auto-initiated from `config.telegram_bot_token` the first time it's
needed - nothing for an end user to click to "connect" at that layer.

What IS per-user is the destination chat_id, stored in user_settings.py.
A user "connects" by pressing Start on the bot in Telegram, picking their
own chat out of `get_recent_chats()` in the Settings UI, then proving they
actually control it via `send_verification_code()` + a code entered back
into the app - `get_recent_chats()` surfaces EVERY chat that recently
messaged the shared bot, not just the current app user's, so picking from
that list alone isn't proof of ownership. The verification round-trip is.

`send_owner_alert()` is the ONLY function in this codebase allowed to
trigger a send - a single, narrow, auditable choke point (same design
principle as safety_rules.py being the sole TRASH gate). No recipient
parameter exists on it; the destination is always resolved from
user_settings.
"""
from __future__ import annotations

import secrets

from src.composio_service import TELEGRAM_TOOLKIT, ComposioService, ComposioServiceError
from src.config import AppConfig
from src.logger import get_logger
from src.user_settings import TelegramSettingsStoreError, load_telegram_settings

logger = get_logger(__name__)

MAX_ALERT_LENGTH = 4096  # Telegram's own text-message cap
BOT_USER_ID = "gmail-cleanup-agent-telegram-bot"  # fixed, app-level - not a Streamlit user_id


class TelegramAlertError(Exception):
    """Base for every alert-send rejection. Messages are always safe to
    show the user directly - never include the bot token."""


class TelegramNotConfiguredError(TelegramAlertError):
    """Deployment-level: auth config / bot token not set up at all."""


class TelegramNotAuthorizedError(TelegramAlertError):
    """This user hasn't enabled alerts / picked a chat yet."""


class TelegramCategoryDisabledError(TelegramAlertError):
    """This specific alert category is turned off in the user's preferences."""


def _bot_composio(config: AppConfig) -> ComposioService:
    return ComposioService(
        config, user_id=BOT_USER_ID,
        toolkit=TELEGRAM_TOOLKIT, auth_config_id=config.composio_telegram_auth_config_id,
    )


def ensure_bot_connected(config: AppConfig) -> str:
    """Idempotent - connects the bot if not already ACTIVE, returns the
    connected_account_id either way."""
    composio = _bot_composio(config)
    info = composio.get_connection_status()
    if info.status == "ACTIVE":
        return info.connected_account_id
    info = composio.initiate_api_key_connection(config.telegram_bot_token)
    if info.status != "ACTIVE":
        raise TelegramNotConfiguredError(f"Could not connect the Telegram bot (status: {info.status}).")
    return info.connected_account_id


def get_recent_chats(config: AppConfig) -> list[dict]:
    """Chats that have recently messaged the bot, for a user to pick their
    own from - never auto-selected. Requires the user to have pressed
    Start on the bot within Telegram's ~24h update-retention window."""
    connected_account_id = ensure_bot_connected(config)
    composio = _bot_composio(config)
    result = composio.execute(
        "TELEGRAM_GET_UPDATES", arguments={"limit": 50},
        connected_account_id=connected_account_id,
    )
    updates = result.get("data", result)
    if isinstance(updates, dict):
        updates = updates.get("result") or updates.get("updates") or []
    if not isinstance(updates, list):
        updates = []

    seen: dict[str, str] = {}
    for update in updates:
        message = update.get("message") or update.get("edited_message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            continue
        label = (f"@{chat['username']}" if chat.get("username") else chat.get("first_name")) or str(chat_id)
        seen[str(chat_id)] = label
    return [{"chat_id": cid, "label": label} for cid, label in seen.items()]


def send_verification_code(config: AppConfig, chat_id: str) -> str:
    """Sends a random 6-digit code to `chat_id` via the shared bot. The app
    user must read that code from their own Telegram app and type it back
    in - proof they have access to that chat's messages, not just that it
    appeared in the shared recent-senders list. Returns the code for the
    caller to compare against what's entered (not stored server-side; the
    caller holds it in session state for the duration of this one check)."""
    code = f"{secrets.randbelow(1_000_000):06d}"
    try:
        connected_account_id = ensure_bot_connected(config)
    except ComposioServiceError as exc:
        raise TelegramNotConfiguredError(f"Telegram bot connection failed: {exc}") from exc

    composio = _bot_composio(config)
    try:
        composio.execute(
            "TELEGRAM_SEND_MESSAGE",
            arguments={"chat_id": chat_id, "text": f"Your Cleanup Agent verification code: {code}"},
            connected_account_id=connected_account_id,
        )
    except ComposioServiceError as exc:
        raise TelegramAlertError(f"Could not send verification code: {exc}") from exc
    return code


def send_owner_alert(config: AppConfig, user_id: str, category: str, message: str) -> None:
    """category: one of user_settings.ALERT_CATEGORIES keys (including
    "daily_summary", gated by its own checkbox there like any other), or
    "test" - the one category that bypasses the checkbox, since it's the
    Settings page's "Send Test Alert" diagnostic, not a real preference."""
    if not message.strip():
        raise TelegramAlertError("Alert message is empty.")
    if len(message) > MAX_ALERT_LENGTH:
        raise TelegramAlertError(f"Alert message exceeds {MAX_ALERT_LENGTH} characters.")

    if not config.composio_telegram_auth_config_id or not config.telegram_bot_token:
        raise TelegramNotConfiguredError("Telegram integration is not configured for this deployment.")

    # Fail closed: a settings-store error is NOT the same as "no settings
    # found" and must never be treated as safe-to-default. If we can't
    # positively verify this user's authorization, refuse to send - never
    # fall back to stale/cached data or another user's settings.
    try:
        settings = load_telegram_settings(config, user_id)
    except TelegramSettingsStoreError as exc:
        raise TelegramNotAuthorizedError(f"Could not verify Telegram authorization right now: {exc}") from exc
    if not settings.enabled or not settings.chat_id:
        raise TelegramNotAuthorizedError("Telegram alerts are not enabled/authorized. Set them up in Telegram Alerts settings.")
    if category != "test" and not settings.category_enabled(category):
        raise TelegramCategoryDisabledError(f"'{category}' alerts are turned off in your Telegram preferences.")

    try:
        connected_account_id = ensure_bot_connected(config)
    except ComposioServiceError as exc:
        raise TelegramNotConfiguredError(f"Telegram bot connection failed: {exc}") from exc

    composio = _bot_composio(config)
    try:
        composio.execute(
            "TELEGRAM_SEND_MESSAGE",
            arguments={"chat_id": settings.chat_id, "text": message},
            connected_account_id=connected_account_id,
        )
    except ComposioServiceError as exc:
        raise TelegramAlertError(f"Send failed: {exc}") from exc
