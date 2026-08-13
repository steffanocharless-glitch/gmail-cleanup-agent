from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from tests.conftest import make_email

from src.classifier import EmailClassifier
from src.config import Category


def _fake_response(payload_json: str, prompt_tokens=10, candidates_tokens=5):
    return SimpleNamespace(
        text=payload_json,
        usage_metadata=SimpleNamespace(
            prompt_token_count=prompt_tokens, candidates_token_count=candidates_tokens
        ),
    )


@patch("src.classifier.genai.Client")
def test_classifier_parses_valid_response(mock_client_cls, config, tmp_path):
    mock_client = MagicMock()
    mock_client_cls.return_value = mock_client
    email = make_email(message_id="m1", sender="a@b.com", subject="Hi")
    mock_client.models.generate_content.return_value = _fake_response(
        '[{"message_id": "m1", "category": "Personal", "confidence": 0.82, "reason": "friendly tone"}]'
    )

    classifier = EmailClassifier(config, cache_dir=tmp_path)
    results = classifier.classify([email])

    assert len(results) == 1
    assert results[0].category == Category.PERSONAL
    assert results[0].confidence == 0.82
    assert classifier.usage.calls == 1


@patch("src.classifier.genai.Client")
def test_classifier_falls_back_to_uncertain_on_invalid_category(mock_client_cls, config, tmp_path):
    mock_client = MagicMock()
    mock_client_cls.return_value = mock_client
    email = make_email(message_id="m2")
    mock_client.models.generate_content.return_value = _fake_response(
        '[{"message_id": "m2", "category": "NotARealCategory", "confidence": 0.9, "reason": "x"}]'
    )

    classifier = EmailClassifier(config, cache_dir=tmp_path)
    results = classifier.classify([email])

    assert results[0].category == Category.UNCERTAIN


@patch("src.classifier.genai.Client")
def test_classifier_falls_back_to_uncertain_on_api_error(mock_client_cls, config, tmp_path):
    mock_client = MagicMock()
    mock_client_cls.return_value = mock_client
    mock_client.models.generate_content.side_effect = RuntimeError("API down")
    email = make_email(message_id="m3")

    classifier = EmailClassifier(config, cache_dir=tmp_path)
    results = classifier.classify([email])

    assert results[0].category == Category.UNCERTAIN
    assert results[0].confidence == 0.0


@patch("src.classifier.genai.Client")
def test_classifier_falls_back_to_uncertain_on_rate_limit(mock_client_cls, config, tmp_path):
    mock_client = MagicMock()
    mock_client_cls.return_value = mock_client
    mock_client.models.generate_content.side_effect = RuntimeError("429 RESOURCE_EXHAUSTED: rate limit")
    email = make_email(message_id="m5")

    classifier = EmailClassifier(config, cache_dir=tmp_path)
    results = classifier.classify([email])

    assert results[0].category == Category.UNCERTAIN
    assert results[0].confidence == 0.0


@patch("src.classifier.genai.Client")
def test_classifier_falls_back_to_uncertain_on_malformed_json(mock_client_cls, config, tmp_path):
    mock_client = MagicMock()
    mock_client_cls.return_value = mock_client
    email = make_email(message_id="m6")
    mock_client.models.generate_content.return_value = _fake_response("not valid json{{{")

    classifier = EmailClassifier(config, cache_dir=tmp_path)
    results = classifier.classify([email])

    assert results[0].category == Category.UNCERTAIN


@patch("src.classifier.genai.Client")
def test_classifier_uses_cache_on_second_call(mock_client_cls, config, tmp_path):
    mock_client = MagicMock()
    mock_client_cls.return_value = mock_client
    email = make_email(message_id="m4", sender="a@b.com", subject="Same subject")
    mock_client.models.generate_content.return_value = _fake_response(
        '[{"message_id": "m4", "category": "Work", "confidence": 0.85, "reason": "x"}]'
    )

    classifier = EmailClassifier(config, cache_dir=tmp_path)
    classifier.classify([email])
    assert mock_client.models.generate_content.call_count == 1

    email_again = make_email(message_id="m4-dup", sender="a@b.com", subject="Same subject")
    classifier.classify([email_again])
    assert mock_client.models.generate_content.call_count == 1  # cache hit, no second API call
