import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from src.redis_store import RedisStoreError, UpstashRedisStore


def _mock_response(payload: dict):
    mock = MagicMock()
    mock.read.return_value = json.dumps(payload).encode("utf-8")
    mock.__enter__.return_value = mock
    mock.__exit__.return_value = False
    return mock


@patch("src.redis_store.urllib.request.urlopen")
def test_get_returns_result(mock_urlopen):
    mock_urlopen.return_value = _mock_response({"result": '{"chat_id": "123"}'})
    store = UpstashRedisStore("https://fake.upstash.io", "token")

    value = store.get("some-key")

    assert value == '{"chat_id": "123"}'
    request = mock_urlopen.call_args.args[0]
    assert request.headers.get("Authorization") == "Bearer token"
    assert json.loads(request.data) == ["GET", "some-key"]


@patch("src.redis_store.urllib.request.urlopen")
def test_get_missing_key_returns_none(mock_urlopen):
    mock_urlopen.return_value = _mock_response({"result": None})
    store = UpstashRedisStore("https://fake.upstash.io", "token")

    assert store.get("missing-key") is None


@patch("src.redis_store.urllib.request.urlopen")
def test_set_sends_correct_command(mock_urlopen):
    mock_urlopen.return_value = _mock_response({"result": "OK"})
    store = UpstashRedisStore("https://fake.upstash.io", "token")

    store.set("some-key", "some-value")

    request = mock_urlopen.call_args.args[0]
    assert json.loads(request.data) == ["SET", "some-key", "some-value"]


@patch("src.redis_store.urllib.request.urlopen")
def test_upstash_error_response_raises(mock_urlopen):
    mock_urlopen.return_value = _mock_response({"error": "WRONGPASS invalid token"})
    store = UpstashRedisStore("https://fake.upstash.io", "bad-token")

    with pytest.raises(RedisStoreError):
        store.get("some-key")


@patch("src.redis_store.urllib.request.urlopen")
def test_network_failure_raises_redis_store_error(mock_urlopen):
    mock_urlopen.side_effect = urllib.error.URLError("connection refused")
    store = UpstashRedisStore("https://fake.upstash.io", "token")

    with pytest.raises(RedisStoreError):
        store.get("some-key")


@patch("src.redis_store.urllib.request.urlopen")
def test_timeout_raises_redis_store_error(mock_urlopen):
    mock_urlopen.side_effect = TimeoutError("timed out")
    store = UpstashRedisStore("https://fake.upstash.io", "token")

    with pytest.raises(RedisStoreError):
        store.set("some-key", "some-value")


@patch("src.redis_store.urllib.request.urlopen")
def test_malformed_json_response_raises_redis_store_error(mock_urlopen):
    mock = MagicMock()
    mock.read.return_value = b"not valid json{{{"
    mock.__enter__.return_value = mock
    mock.__exit__.return_value = False
    mock_urlopen.return_value = mock
    store = UpstashRedisStore("https://fake.upstash.io", "token")

    with pytest.raises(RedisStoreError):
        store.get("some-key")
