from src.identity import derive_user_id


def test_same_pair_produces_same_id():
    a = derive_user_id("alice@example.com", "hunter2")
    b = derive_user_id("alice@example.com", "hunter2")
    assert a == b


def test_different_passcode_produces_different_id():
    a = derive_user_id("alice@example.com", "hunter2")
    b = derive_user_id("alice@example.com", "different")
    assert a != b


def test_different_identifier_produces_different_id():
    a = derive_user_id("alice@example.com", "hunter2")
    b = derive_user_id("bob@example.com", "hunter2")
    assert a != b


def test_identifier_case_and_whitespace_normalized():
    a = derive_user_id("Alice@Example.com", "hunter2")
    b = derive_user_id("  alice@example.com  ", "hunter2")
    assert a == b


def test_passcode_case_sensitive():
    a = derive_user_id("alice@example.com", "hunter2")
    b = derive_user_id("alice@example.com", "Hunter2")
    assert a != b


def test_output_is_hex_sha256():
    result = derive_user_id("alice@example.com", "hunter2")
    assert len(result) == 64
    int(result, 16)  # raises if not valid hex
