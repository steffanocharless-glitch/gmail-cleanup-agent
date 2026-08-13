import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import AppConfig, ConfidenceThresholds  # noqa: E402
from src.models import EmailMetadata  # noqa: E402


@pytest.fixture
def config():
    cfg = AppConfig()
    cfg.composio_api_key = "test-key"
    cfg.composio_gmail_auth_config_id = "test-auth-config"
    cfg.gemini_api_key = "test-key"
    cfg.thresholds = ConfidenceThresholds(high=0.90, low=0.70)
    cfg.dry_run = True
    return cfg


def make_email(
    message_id="msg-1",
    sender="promo@shop.example.com",
    subject="50% off sale",
    snippet="Limited time offer",
    days_old=45,
    unsubscribe=True,
    unread=False,
) -> EmailMetadata:
    return EmailMetadata(
        message_id=message_id,
        thread_id="thread-1",
        sender=sender,
        subject=subject,
        snippet=snippet,
        received_at=datetime.now(timezone.utc) - timedelta(days=days_old),
        label_ids=["INBOX"],
        has_attachment=False,
        unsubscribe_header=unsubscribe,
        is_unread=unread,
    )
