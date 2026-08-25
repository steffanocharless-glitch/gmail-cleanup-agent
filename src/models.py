"""Shared data models used across the application."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class EmailMetadata:
    """Lightweight metadata fetched for classification. No full body by default."""
    message_id: str
    thread_id: str
    sender: str
    subject: str
    snippet: str
    received_at: datetime
    label_ids: list[str] = field(default_factory=list)
    has_attachment: bool = False
    unsubscribe_header: bool = False
    is_unread: bool = True
    full_body: Optional[str] = None

    @property
    def age_days(self) -> int:
        delta = datetime.now(self.received_at.tzinfo) - self.received_at
        return max(delta.days, 0)


@dataclass
class ClassificationResult:
    """`category` is the Level-1 purpose (what the email does - see
    config.Category). `context` is the Level-2 sender/domain (e.g. "Banking",
    "Amazon") - informational only, never used to decide `category`."""
    message_id: str
    category: str
    confidence: float
    reason: str
    source: str  # "rule" or "gemini"
    context: str = ""
    detailed_type: str = ""
    is_promotional: bool = False
    is_security_sensitive: bool = False
    cleanup_safe: bool = False

    @property
    def display_category(self) -> str:
        return f"{self.category} / {self.context}" if self.context else self.category


@dataclass
class RecommendedAction:
    message_id: str
    email: EmailMetadata
    classification: ClassificationResult
    recommended_action: str
    final_action: Optional[str] = None
    protected: bool = False
    protection_reason: Optional[str] = None
    user_override: Optional[str] = None


@dataclass
class CleanupRecord:
    timestamp: str
    user_identifier: str
    message_id: str
    sender: str
    subject: str
    classification: str
    confidence: float
    recommended_action: str
    final_action: str
    reason: str
    error: Optional[str] = None


@dataclass
class ScanResult:
    scanned_count: int
    total_inbox_count: int
    unread_count: int
    date_range_start: Optional[datetime]
    date_range_end: Optional[datetime]
    recommendations: list[RecommendedAction] = field(default_factory=list)


@dataclass
class CleanupSummary:
    scanned: int = 0
    classified: int = 0
    archived: int = 0
    trashed: int = 0
    labeled: int = 0
    protected: int = 0
    skipped: int = 0
    manual_review: int = 0
    errors: int = 0
    error_details: list[str] = field(default_factory=list)
