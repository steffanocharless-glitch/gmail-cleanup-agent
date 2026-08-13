"""Application logging and append-only audit log for cleanup actions."""
from __future__ import annotations

import csv
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from src.config import CLEANUP_LOG_DIR
from src.models import CleanupRecord

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


_AUDIT_FIELDS = [
    "timestamp", "user_identifier", "message_id", "sender", "subject",
    "classification", "confidence", "recommended_action", "final_action",
    "reason", "error",
]


class AuditLogger:
    """Appends one CSV row per cleanup action. Never stores email bodies."""

    def __init__(self, user_identifier: str, log_dir: Path = CLEANUP_LOG_DIR):
        self.user_identifier = user_identifier
        log_dir.mkdir(parents=True, exist_ok=True)
        safe_id = "".join(c if c.isalnum() or c in "@._-" else "_" for c in user_identifier)
        date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        self.path = log_dir / f"cleanup_{safe_id}_{date_str}.csv"
        self._ensure_header()

    def _ensure_header(self) -> None:
        if not self.path.exists():
            with self.path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=_AUDIT_FIELDS)
                writer.writeheader()

    def record(self, record: CleanupRecord) -> None:
        with self.path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=_AUDIT_FIELDS)
            writer.writerow(asdict(record))

    def log_action(
        self,
        message_id: str,
        sender: str,
        subject: str,
        classification: str,
        confidence: float,
        recommended_action: str,
        final_action: str,
        reason: str,
        error: str | None = None,
    ) -> None:
        self.record(CleanupRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            user_identifier=self.user_identifier,
            message_id=message_id,
            sender=sender,
            subject=subject[:200],
            classification=classification,
            confidence=confidence,
            recommended_action=recommended_action,
            final_action=final_action,
            reason=reason[:300],
            error=error,
        ))
