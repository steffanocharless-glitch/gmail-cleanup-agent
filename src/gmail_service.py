"""Gmail-specific operations built on top of ComposioService.

Fetches only lightweight metadata by default (sender/subject/snippet/labels/
timestamp/attachment flag). Full body is fetched separately and only when
the classifier explicitly needs it.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from src.composio_service import ComposioService
from src.logger import get_logger
from src.models import EmailMetadata

logger = get_logger(__name__)

INBOX_LABEL = "INBOX"
TRASH_LABEL = "TRASH"


class GmailService:
    def __init__(self, composio: ComposioService, connected_account_id: Optional[str] = None):
        self._composio = composio
        self._connected_account_id = connected_account_id

    # ---- Profile / overview ------------------------------------------------

    def get_profile(self) -> dict:
        return self._composio.execute(
            "GMAIL_GET_PROFILE", arguments={}, connected_account_id=self._connected_account_id
        )

    def list_labels(self) -> list[dict]:
        result = self._composio.execute(
            "GMAIL_LIST_LABELS",
            arguments={"include_details": True},
            connected_account_id=self._connected_account_id,
        )
        return result.get("labels", [])

    def ensure_label(self, label_name: str) -> str:
        """Return the Gmail label ID for label_name, creating it if needed."""
        for label in self.list_labels():
            if label.get("name", "").lower() == label_name.lower():
                return label["id"]
        result = self._composio.execute(
            "GMAIL_CREATE_LABEL",
            arguments={"label_name": label_name},
            connected_account_id=self._connected_account_id,
        )
        return result.get("id") or result.get("label_id")

    # ---- Fetching -----------------------------------------------------------

    def fetch_inbox_metadata(self, max_results: int, query: Optional[str] = None) -> list[EmailMetadata]:
        """Page through the inbox collecting lightweight metadata only.

        `query` is a raw Gmail search query (e.g. "after:2026/01/01
        before:2026/02/01") - forwarded as-is to GMAIL_FETCH_EMAILS.
        """
        collected: list[EmailMetadata] = []
        page_token = None

        while len(collected) < max_results:
            batch_size = min(100, max_results - len(collected))
            arguments = {
                "label_ids": [INBOX_LABEL],
                "max_results": batch_size,
                "page_token": page_token,
                "include_payload": False,
                "verbose": True,
            }
            if query:
                arguments["query"] = query
            result = self._composio.execute(
                "GMAIL_FETCH_EMAILS",
                arguments=arguments,
                connected_account_id=self._connected_account_id,
            )
            messages = result.get("messages", [])
            if not messages:
                break
            for msg in messages:
                collected.append(_to_metadata(msg))
            page_token = result.get("nextPageToken") or result.get("next_page_token")
            if not page_token:
                break

        return collected[:max_results]

    def fetch_full_body(self, message_id: str) -> str:
        result = self._composio.execute(
            "GMAIL_FETCH_MESSAGE_BY_ID",
            arguments={"message_id": message_id, "format": "full"},
            connected_account_id=self._connected_account_id,
        )
        return result.get("body", "") or result.get("snippet", "")

    # ---- Mutations ------------------------------------------------------------

    def archive_batch(self, message_ids: list[str]) -> list[str]:
        """Remove INBOX label from messages (archive). Returns failed IDs."""
        return self._batch_modify(message_ids, add_label_ids=[], remove_label_ids=[INBOX_LABEL])

    def add_label_batch(self, message_ids: list[str], label_id: str) -> list[str]:
        return self._batch_modify(message_ids, add_label_ids=[label_id], remove_label_ids=[])

    def _batch_modify(
        self, message_ids: list[str], add_label_ids: list[str], remove_label_ids: list[str]
    ) -> list[str]:
        failed: list[str] = []
        chunk_size = 1000
        for i in range(0, len(message_ids), chunk_size):
            chunk = message_ids[i:i + chunk_size]
            try:
                self._composio.execute(
                    "GMAIL_BATCH_MODIFY_MESSAGES",
                    arguments={
                        "message_ids": chunk,
                        "add_label_ids": add_label_ids,
                        "remove_label_ids": remove_label_ids,
                    },
                    connected_account_id=self._connected_account_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("Batch modify failed for chunk of %d: %s", len(chunk), exc)
                failed.extend(chunk)
        return failed

    def trash_batch(self, message_ids: list[str]) -> list[str]:
        """Gmail has no bulk-trash tool; trash one at a time. Returns failed IDs."""
        failed: list[str] = []
        for message_id in message_ids:
            try:
                self._composio.execute(
                    "GMAIL_MOVE_TO_TRASH",
                    arguments={"message_id": message_id},
                    connected_account_id=self._connected_account_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("Trash failed for %s: %s", message_id, exc)
                failed.append(message_id)
        return failed


def _to_metadata(raw: dict) -> EmailMetadata:
    headers = {h.get("name", "").lower(): h.get("value", "") for h in raw.get("payload_headers", [])} \
        if raw.get("payload_headers") else {}
    sender = raw.get("sender") or headers.get("from", "unknown")
    subject = raw.get("subject") or headers.get("subject", "(no subject)")

    received_at = None
    timestamp_str = raw.get("messageTimestamp") or raw.get("message_timestamp")
    if timestamp_str:
        try:
            received_at = datetime.fromisoformat(str(timestamp_str).replace("Z", "+00:00"))
        except ValueError:
            received_at = None
    if received_at is None:
        timestamp_ms = raw.get("internal_date") or raw.get("internalDate")
        if timestamp_ms:
            received_at = datetime.fromtimestamp(int(timestamp_ms) / 1000, tz=timezone.utc)
        else:
            received_at = datetime.now(timezone.utc)

    label_ids = raw.get("label_ids") or raw.get("labelIds") or []

    message_id = raw.get("messageId") or raw.get("message_id") or raw.get("id", "")
    thread_id = raw.get("threadId") or raw.get("thread_id", "")

    preview = raw.get("preview") or {}
    snippet = raw.get("snippet") or preview.get("body") or (raw.get("messageText") or "")[:300]

    has_attachment = bool(
        raw.get("attachmentList") or raw.get("has_attachment") or raw.get("attachment_list")
    )
    unsubscribe = bool(headers.get("list-unsubscribe"))

    return EmailMetadata(
        message_id=message_id,
        thread_id=thread_id,
        sender=sender,
        subject=subject,
        snippet=snippet,
        received_at=received_at,
        label_ids=label_ids,
        has_attachment=has_attachment,
        unsubscribe_header=unsubscribe,
        is_unread="UNREAD" in label_ids,
    )
