"""Deterministic user identity derivation for persistent Gmail connections.

Streamlit session_state does not survive a page reload or a new browser -
without a durable key, every visit would look like a brand-new user and
force a fresh Gmail OAuth. Composio itself is the durable store (it already
remembers connected accounts by user_id); the only missing piece is a way
for a returning visitor to reproduce the same user_id.

This is a lightweight session key, not verified identity: knowing the same
identifier+passcode pair reproduces the same user_id and therefore the same
Gmail connection, on any device. There is no password reset, no identity
verification, and no protection against someone who is told (or guesses)
both values.
"""
from __future__ import annotations

import hashlib


def derive_user_id(identifier: str, passcode: str) -> str:
    raw = f"{identifier.strip().lower()}:{passcode}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
