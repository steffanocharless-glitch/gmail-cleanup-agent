"""Minimal Upstash Redis REST client - stdlib only, no new dependency.

Upstash's REST API (https://upstash.com/docs/redis/features/restapi) takes
a JSON command array POSTed to the database URL with a Bearer token, and
returns {"result": ...} or {"error": ...}. Only GET/SET are needed here -
this is not a general Redis client.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Optional


class RedisStoreError(Exception):
    """Redis unreachable, timed out, or returned an error - never silently
    swallowed into a default value by this module. Callers decide what
    "can't verify" should mean for them."""


class UpstashRedisStore:
    def __init__(self, url: str, token: str, timeout: float = 5.0):
        self._url = url.rstrip("/")
        self._token = token
        self._timeout = timeout

    def get(self, key: str) -> Optional[str]:
        return self._command("GET", key)

    def set(self, key: str, value: str) -> None:
        self._command("SET", key, value)

    def _command(self, *args: str) -> Optional[str]:
        body = json.dumps(list(args)).encode("utf-8")
        request = urllib.request.Request(
            self._url, data=body, method="POST",
            headers={"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise RedisStoreError(f"Upstash request failed: {exc}") from exc
        if isinstance(payload, dict) and payload.get("error"):
            raise RedisStoreError(f"Upstash error: {payload['error']}")
        return payload.get("result") if isinstance(payload, dict) else None
