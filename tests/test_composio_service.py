from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from composio.exceptions import ComposioMultipleConnectedAccountsError

from src.composio_service import CALENDAR_TOOLKIT, ComposioService, ComposioServiceError
from src.config import AppConfig


def _make_service():
    cfg = AppConfig()
    cfg.composio_api_key = "test-key"
    cfg.gmail_retry_attempts = 1
    cfg.gmail_retry_backoff_seconds = 0
    service = ComposioService(cfg, user_id="user-1", toolkit=CALENDAR_TOOLKIT, auth_config_id="cal_auth_1")
    service._client = MagicMock()
    return service


def _account(status, email=None, acc_id="acc-1", toolkit="GOOGLECALENDAR"):
    acc = SimpleNamespace(id=acc_id, status=status, toolkit=SimpleNamespace(slug=toolkit))
    if email:
        acc.connected_account_email = email
    return acc


def test_one_active_plus_one_expired_reuses_the_active_one():
    service = _make_service()
    # statuses=["ACTIVE"] is passed to the API, but the client-side filter
    # is what actually protects this - simulate the API returning an
    # EXPIRED account anyway (e.g. a stale/misbehaving filter) to prove it.
    service._client.connected_accounts.list.return_value = SimpleNamespace(items=[
        _account("ACTIVE", email="a@x.com", acc_id="acc-active"),
        _account("EXPIRED", email="a@x.com", acc_id="acc-expired"),
    ])

    info = service.get_connection_status()

    assert info.status == "ACTIVE"
    assert info.connected_account_id == "acc-active"
    assert info.other_active_candidates == []


def test_one_active_only():
    service = _make_service()
    service._client.connected_accounts.list.return_value = SimpleNamespace(items=[
        _account("ACTIVE", email="a@x.com", acc_id="acc-1"),
    ])

    info = service.get_connection_status()

    assert info.status == "ACTIVE"
    assert info.connected_account_id == "acc-1"


def test_zero_active_accounts():
    service = _make_service()
    service._client.connected_accounts.list.return_value = SimpleNamespace(items=[])

    info = service.get_connection_status()

    assert info.status == "NOT_CONNECTED"
    assert info.connected_account_id is None


def test_multiple_active_prefers_matching_email():
    service = _make_service()
    service._client.connected_accounts.list.return_value = SimpleNamespace(items=[
        _account("ACTIVE", email="other@x.com", acc_id="acc-other"),
        _account("ACTIVE", email="mine@x.com", acc_id="acc-mine"),
    ])

    info = service.get_connection_status(prefer_email="mine@x.com")

    assert info.connected_account_id == "acc-mine"
    assert len(info.other_active_candidates) == 1
    assert info.other_active_candidates[0].connected_account_id == "acc-other"


def test_multiple_active_no_email_hint_falls_back_to_first():
    service = _make_service()
    service._client.connected_accounts.list.return_value = SimpleNamespace(items=[
        _account("ACTIVE", email="first@x.com", acc_id="acc-first"),
        _account("ACTIVE", email="second@x.com", acc_id="acc-second"),
    ])

    info = service.get_connection_status()

    assert info.connected_account_id == "acc-first"
    assert len(info.other_active_candidates) == 1


def test_start_connection_reuses_active_without_calling_link():
    service = _make_service()
    service._client.connected_accounts.list.return_value = SimpleNamespace(items=[
        _account("ACTIVE", email="a@x.com", acc_id="acc-1"),
    ])

    info = service.start_connection()

    assert info.status == "ACTIVE"
    assert info.connected_account_id == "acc-1"
    service._client.connected_accounts.link.assert_not_called()


def test_start_connection_calls_link_when_nothing_active():
    service = _make_service()
    service._client.connected_accounts.list.return_value = SimpleNamespace(items=[])
    service._client.connected_accounts.link.return_value = SimpleNamespace(
        id="acc-new", redirect_url="https://example.com/authorize"
    )

    info = service.start_connection()

    assert info.status == "INITIATED"
    assert info.redirect_url == "https://example.com/authorize"
    service._client.connected_accounts.link.assert_called_once()


def test_reconnect_after_disconnect_calls_link_again():
    """Disconnect leaves zero ACTIVE accounts - the next start_connection()
    must go through link() again, not get stuck thinking one still exists."""
    service = _make_service()
    service._client.connected_accounts.list.return_value = SimpleNamespace(items=[])
    service._client.connected_accounts.link.return_value = SimpleNamespace(
        id="acc-new2", redirect_url="https://example.com/authorize2"
    )

    info = service.start_connection()

    assert info.status == "INITIATED"
    service._client.connected_accounts.link.assert_called_once()


def test_start_connection_survives_multiple_accounts_error_by_reusing():
    """Belt-and-suspenders path: our own pre-check somehow missed it (race),
    link() itself raises - re-check and reuse instead of crashing."""
    service = _make_service()
    service._client.connected_accounts.list.side_effect = [
        SimpleNamespace(items=[]),  # pre-check: nothing found (stale)
        SimpleNamespace(items=[_account("ACTIVE", email="a@x.com", acc_id="acc-recovered")]),  # re-check after error
    ]
    service._client.connected_accounts.link.side_effect = ComposioMultipleConnectedAccountsError("dup")

    info = service.start_connection()

    assert info.status == "ACTIVE"
    assert info.connected_account_id == "acc-recovered"


def test_start_connection_raises_friendly_error_when_truly_stuck():
    service = _make_service()
    service._client.connected_accounts.list.return_value = SimpleNamespace(items=[])
    service._client.connected_accounts.link.side_effect = ComposioMultipleConnectedAccountsError("dup")

    with pytest.raises(ComposioServiceError):
        service.start_connection()
