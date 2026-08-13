"""Gemini-based classification for emails the rule engine can't confidently
resolve. Only sender/subject/snippet metadata is sent - never full bodies
unless a caller explicitly opts in via `include_body`.

Uses the official `google-genai` package (`from google import genai`), not
the deprecated `google-generativeai`. Gemini never touches Gmail directly -
it only returns a category/confidence/reason; safety_rules.py and
cleanup_engine.py remain the sole decision-makers for any cleanup action.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from google import genai
from google.genai import types

from src.config import AppConfig, CACHE_DIR, Category
from src.logger import get_logger
from src.models import ClassificationResult, EmailMetadata

logger = get_logger(__name__)

VALID_CATEGORIES = set(Category.ALL)

SYSTEM_PROMPT = f"""You are an email triage classifier. For each email you receive
(sender, subject, snippet, whether it has an attachment), assign exactly one
category from this fixed list:

{", ".join(VALID_CATEGORIES)}

Rules:
- If you are not confident, use "Uncertain" with a lower confidence score.
- Never guess "Spam / Useless" unless clearly unsolicited bulk mail with no
  legitimate business purpose.
- Financial, legal, HR, client, and security-related mail should lean toward
  "Finance", "Client", "Requires Action", or "Important" rather than being
  dismissed as promotional.

Respond with ONLY a JSON array, one object per input email in the same order,
each with keys: "message_id", "category", "confidence" (0.0-1.0 float),
"reason" (short string, <15 words). No prose, no markdown fences."""


class ClassifierUsageTracker:
    def __init__(self):
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0

    def add(self, usage_metadata) -> None:
        self.calls += 1
        self.input_tokens += getattr(usage_metadata, "prompt_token_count", 0) or 0
        self.output_tokens += getattr(usage_metadata, "candidates_token_count", 0) or 0

    def as_dict(self) -> dict:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }


class EmailClassifier:
    def __init__(self, config: AppConfig, cache_dir: Path = CACHE_DIR):
        if not config.gemini_api_key:
            raise RuntimeError("GEMINI_API_KEY is not configured")
        self._client = genai.Client(api_key=config.gemini_api_key)
        self._model = config.gemini_model
        self._batch_size = config.classify_batch_size
        self._cache_path = cache_dir / "classification_cache.json"
        self._cache = self._load_cache()
        self.usage = ClassifierUsageTracker()

    def classify(self, emails: list[EmailMetadata]) -> list[ClassificationResult]:
        results: list[ClassificationResult] = []
        to_classify: list[EmailMetadata] = []

        for email in emails:
            cache_key = self._cache_key(email)
            cached = self._cache.get(cache_key)
            if cached:
                results.append(ClassificationResult(
                    message_id=email.message_id, source="gemini", **cached
                ))
            else:
                to_classify.append(email)

        for i in range(0, len(to_classify), self._batch_size):
            batch = to_classify[i:i + self._batch_size]
            results.extend(self._classify_batch(batch))

        self._save_cache()
        return results

    def _classify_batch(self, batch: list[EmailMetadata]) -> list[ClassificationResult]:
        payload = [
            {
                "message_id": e.message_id,
                "sender": e.sender,
                "subject": e.subject,
                "snippet": e.snippet[:300],
                "has_attachment": e.has_attachment,
                "age_days": e.age_days,
            }
            for e in batch
        ]

        try:
            response = self._client.models.generate_content(
                model=self._model,
                contents=json.dumps(payload),
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    response_mime_type="application/json",
                ),
            )
            self.usage.add(getattr(response, "usage_metadata", None))
            text = response.text
            if not text:
                raise ValueError("empty response from Gemini")
            parsed = json.loads(text)
        except Exception as exc:  # noqa: BLE001 - covers API errors, rate limits, bad JSON
            logger.error("Gemini classification batch failed: %s", exc)
            return [self._fallback(e, str(exc)) for e in batch]

        by_id = {item["message_id"]: item for item in parsed if "message_id" in item}
        results = []
        for email in batch:
            item = by_id.get(email.message_id)
            if not item or item.get("category") not in VALID_CATEGORIES:
                results.append(self._fallback(email, "missing/invalid model output"))
                continue
            try:
                confidence = float(item.get("confidence", 0.5))
            except (TypeError, ValueError):
                results.append(self._fallback(email, "non-numeric confidence"))
                continue
            reason = str(item.get("reason", ""))[:200]
            result = ClassificationResult(
                message_id=email.message_id,
                category=item["category"],
                confidence=confidence,
                reason=reason,
                source="gemini",
            )
            results.append(result)
            self._cache[self._cache_key(email)] = {
                "category": result.category,
                "confidence": result.confidence,
                "reason": result.reason,
            }
        return results

    @staticmethod
    def _fallback(email: EmailMetadata, reason: str) -> ClassificationResult:
        """Gemini failure/timeout/rate-limit/malformed output always lands
        here: Uncertain category, zero confidence. safety_rules.py maps
        that straight to KEEP - Gemini can never cause a deletion."""
        return ClassificationResult(
            message_id=email.message_id,
            category=Category.UNCERTAIN,
            confidence=0.0,
            reason=f"Classification failed, defaulting to Uncertain: {reason}"[:200],
            source="gemini",
        )

    @staticmethod
    def _cache_key(email: EmailMetadata) -> str:
        raw = f"{email.sender.lower()}|{email.subject.strip().lower()}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _load_cache(self) -> dict:
        if self._cache_path.exists():
            try:
                return json.loads(self._cache_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def _save_cache(self) -> None:
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._cache_path.write_text(json.dumps(self._cache), encoding="utf-8")
