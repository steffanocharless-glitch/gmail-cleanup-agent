"""Thin wrapper around the Composio SDK (>=0.19, `pip install composio`).

Handles: connection lifecycle (link/wait/status/disable), and tool execution
with retry + rate-limit handling. No Composio API key or secret is ever
returned to callers/UI - only connection status and data.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Optional

from composio import Composio

from src.config import AppConfig
from src.logger import get_logger

logger = get_logger(__name__)

GMAIL_TOOLKIT = "GMAIL"


class ComposioServiceError(Exception):
    pass


class RateLimitExceeded(ComposioServiceError):
    pass


@dataclass
class ConnectionInfo:
    connected_account_id: Optional[str]
    status: str  # ACTIVE | INITIATED | FAILED | NOT_CONNECTED
    connected_email: Optional[str] = None
    redirect_url: Optional[str] = None


class ComposioService:
    """User-scoped Composio access. One instance per app session/user_id."""

    def __init__(self, config: AppConfig, user_id: str):
        if not config.composio_api_key:
            raise ComposioServiceError("COMPOSIO_API_KEY is not configured")
        self._config = config
        self.user_id = user_id
        self._client = Composio(api_key=config.composio_api_key)

    # ---- Connection lifecycle -------------------------------------------------

    def start_connection(self) -> ConnectionInfo:
        """Kick off hosted OAuth for this user and return a redirect URL."""
        request = self._client.connected_accounts.link(
            user_id=self.user_id,
            auth_config_id=self._config.composio_gmail_auth_config_id,
            callback_url=self._config.composio_callback_url or None,
        )
        return ConnectionInfo(
            connected_account_id=getattr(request, "id", None),
            status="INITIATED",
            redirect_url=getattr(request, "redirect_url", None),
        )

    def wait_for_connection(self, connected_account_id: str, timeout: float = 120.0) -> ConnectionInfo:
        account = self._client.connected_accounts.wait_for_connection(
            connected_account_id, timeout=timeout
        )
        return self._to_connection_info(account)

    def get_connection_status(self) -> ConnectionInfo:
        """Look up the most recent active Gmail connection for this user, if any.

        Retries transient lookup failures instead of reporting them as
        "not connected" - swallowing a network blip into NOT_CONNECTED would
        send an already-connected user through Google's OAuth consent screen
        again for no reason."""
        attempts = max(self._config.gmail_retry_attempts, 1)
        backoff = self._config.gmail_retry_backoff_seconds
        last_error: Optional[Exception] = None

        for attempt in range(1, attempts + 1):
            try:
                accounts = self._client.connected_accounts.list(
                    user_ids=[self.user_id], statuses=["ACTIVE"]
                )
                break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt >= attempts:
                    raise ComposioServiceError(
                        f"Could not verify existing Gmail connection after {attempts} attempts: {exc}"
                    ) from exc
                sleep_for = backoff * (2 ** (attempt - 1))
                logger.warning(
                    "connected_accounts.list attempt %d/%d failed (%s); retrying in %.1fs",
                    attempt, attempts, exc, sleep_for,
                )
                time.sleep(sleep_for)

        items = getattr(accounts, "items", accounts) or []
        gmail_accounts = [
            a for a in items
            if getattr(getattr(a, "toolkit", None), "slug", "").upper() == GMAIL_TOOLKIT
        ]
        if not gmail_accounts:
            return ConnectionInfo(connected_account_id=None, status="NOT_CONNECTED")
        return self._to_connection_info(gmail_accounts[0])

    def disconnect(self, connected_account_id: str) -> None:
        self._client.connected_accounts.disable(connected_account_id)

    @staticmethod
    def _to_connection_info(account: Any) -> ConnectionInfo:
        status = getattr(account, "status", "NOT_CONNECTED")
        email = None
        # Best-effort extraction of the connected email address; SDK shape
        # for connection metadata can vary by toolkit/version.
        for attr in ("connected_account_email", "email"):
            if hasattr(account, attr):
                email = getattr(account, attr)
                break
        return ConnectionInfo(
            connected_account_id=getattr(account, "id", None),
            status=status,
            connected_email=email,
        )

    # ---- Tool execution ---------------------------------------------------

    def execute(self, slug: str, arguments: dict, connected_account_id: Optional[str] = None) -> dict:
        """Execute a Composio tool with retry on transient/rate-limit errors."""
        attempts = max(self._config.gmail_retry_attempts, 1)
        backoff = self._config.gmail_retry_backoff_seconds
        last_error: Optional[Exception] = None

        for attempt in range(1, attempts + 1):
            try:
                kwargs: dict[str, Any] = dict(
                    slug=slug, user_id=self.user_id, arguments=arguments,
                    # Direct tools.execute() requires an explicit toolkit version;
                    # "latest" isn't accepted, so we skip the pin and always run
                    # against whatever version Composio currently serves.
                    dangerously_skip_version_check=True,
                )
                if connected_account_id:
                    kwargs["connected_account_id"] = connected_account_id
                result = self._client.tools.execute(**kwargs)
                # tools.execute() returns a plain dict at runtime (not an
                # object), so index it - getattr() would silently no-op here.
                if isinstance(result, dict):
                    data = result.get("data", result)
                    success = result.get("successful", True)
                    error_msg = result.get("error")
                else:
                    data = getattr(result, "data", result)
                    success = getattr(result, "successful", True)
                    error_msg = getattr(result, "error", None)
                if not success:
                    raise ComposioServiceError(f"{slug} failed: {error_msg or 'unknown tool error'}")
                return data if isinstance(data, dict) else {"data": data}
            except ComposioServiceError:
                raise
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                is_rate_limit = "429" in str(exc) or "rate limit" in str(exc).lower()
                if attempt >= attempts:
                    break
                sleep_for = backoff * (2 ** (attempt - 1))
                logger.warning(
                    "%s attempt %d/%d failed (%s); retrying in %.1fs",
                    slug, attempt, attempts, exc, sleep_for,
                )
                time.sleep(sleep_for)
                if is_rate_limit:
                    continue

        raise ComposioServiceError(f"{slug} failed after {attempts} attempts: {last_error}")
