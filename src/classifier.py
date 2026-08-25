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

from src.config import AppConfig, CACHE_DIR, Category, compute_cleanup_safe
from src.logger import get_logger
from src.models import ClassificationResult, EmailMetadata
from src.rule_engine import derive_context

logger = get_logger(__name__)

VALID_CATEGORIES = set(Category.ALL)

SYSTEM_PROMPT = f"""You are an email triage classifier. Classify each email by
what it DOES (its purpose), never by who sent it - the same sender (a bank,
an online store, a dev platform...) routinely sends mail that belongs in many
different categories, and you must judge each email on its own content.

Priority when reasoning: purpose > content > subject > sender.

For each email you receive (sender, subject, snippet, whether it has an
attachment), assign exactly one category from this fixed list:

{", ".join(VALID_CATEGORIES)}

Rules:
- If you are not confident, use "Uncertain" with a lower confidence score.
- Never guess "Spam / Suspicious" unless clearly unsolicited bulk mail with
  no legitimate business purpose.
- A message mentioning money/loans/credit is NOT automatically a transaction
  or finance email - "Your ₹50,000 pre-approved amount is waiting" is a
  promotion ("Promotions & Marketing"), while "Your payment of ₹5,000 was
  successful" is a transaction ("Transactions"). Judge overall intent, not
  isolated keywords.
- Promotional intent (advertising, upselling, "apply now", "pre-approved",
  discounts, cashback, "shop now") takes priority over incidental
  transactional-sounding language.
- OTPs, security alerts, transaction alerts, statements, bills, KYC/account
  notices, and legal/regulatory mail must never be classified as
  promotional, even if sent by a sender that also sends promotions.
- "detailed_type" is a short free-text label for the specific kind of
  message within its category (e.g. "Debit Alert", "KYC", "Loan Promotion",
  "Pull Request Merged").
- "is_promotional" is true only if the email's primary intent is
  advertising/upselling/cross-selling.
- "is_security_sensitive" is true for OTPs, login/fraud alerts, password
  resets, and any account-security-relevant message.

Respond with ONLY a JSON array, one object per input email in the same order,
each with keys: "message_id", "category", "detailed_type" (short string),
"confidence" (0.0-1.0 float), "reason" (short string, <15 words),
"is_promotional" (bool), "is_security_sensitive" (bool). No prose, no
markdown fences."""


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
        # v2: cache values now carry detailed_type/is_promotional/is_security_sensitive.
        # New filename so a v1 cache from before the purpose-first rework can't be
        # misread as v2-shaped data.
        self._cache_path = cache_dir / "classification_cache_v2.json"
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
                    message_id=email.message_id, source="gemini",
                    context=derive_context(email.sender), **cached
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
            detailed_type = str(item.get("detailed_type", ""))[:100]
            is_promotional = bool(item.get("is_promotional", False))
            is_security_sensitive = bool(item.get("is_security_sensitive", False))
            result = ClassificationResult(
                message_id=email.message_id,
                category=item["category"],
                confidence=confidence,
                reason=reason,
                source="gemini",
                context=derive_context(email.sender),
                detailed_type=detailed_type,
                is_promotional=is_promotional,
                is_security_sensitive=is_security_sensitive,
                cleanup_safe=compute_cleanup_safe(item["category"], is_promotional, is_security_sensitive),
            )
            results.append(result)
            self._cache[self._cache_key(email)] = {
                "category": result.category,
                "confidence": result.confidence,
                "reason": result.reason,
                "detailed_type": result.detailed_type,
                "is_promotional": result.is_promotional,
                "is_security_sensitive": result.is_security_sensitive,
                "cleanup_safe": result.cleanup_safe,
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
            context=derive_context(email.sender),
            cleanup_safe=False,
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
