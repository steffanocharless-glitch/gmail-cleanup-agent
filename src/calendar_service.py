"""Google Calendar operations built on top of ComposioService.

Only creates events - never reads, modifies, or deletes anything else in
the user's calendar. Tool slug/schema confirmed against the live Composio
SDK (GOOGLECALENDAR_BATCH_EVENTS) before use, per project convention.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from src.composio_service import ComposioService
from src.logger import get_logger

logger = get_logger(__name__)


class CalendarService:
    def __init__(self, composio: ComposioService, connected_account_id: Optional[str] = None):
        self._composio = composio
        self._connected_account_id = connected_account_id

    def create_all_day_events(self, events: list[dict]) -> dict:
        """`events` items: {"op_id": str, "summary": str, "description": str,
        "start_date": date, "end_date": date (exclusive, per Google's
        all-day event convention - normally start_date + 1 day)}.
        Returns the raw Composio response data (per-operation results)."""
        operations = [
            {
                "op_id": e["op_id"],
                "method": "POST",
                "calendar_id": "primary",
                "body": {
                    "summary": e["summary"],
                    "description": e.get("description", ""),
                    "start": {"date": _iso(e["start_date"])},
                    "end": {"date": _iso(e["end_date"])},
                },
            }
            for e in events
        ]
        return self._composio.execute(
            "GOOGLECALENDAR_BATCH_EVENTS",
            arguments={"operations": operations, "fail_fast": False},
            connected_account_id=self._connected_account_id,
        )


def _iso(d: date) -> str:
    return d.strftime("%Y-%m-%d")
