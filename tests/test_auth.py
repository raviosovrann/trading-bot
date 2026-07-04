from tradingbot.auth import is_authorized, ip_allowed


def test_correct_token_authorized():
    assert is_authorized("secret", "secret") is True


def test_wrong_token_rejected():
    assert is_authorized("nope", "secret") is False


def test_missing_token_rejected():
    assert is_authorized(None, "secret") is False
    assert is_authorized("", "secret") is False


def test_empty_allowlist_allows_all():
    assert ip_allowed("9.9.9.9", ()) is True


def test_allowlisted_ip_allowed():
    assert ip_allowed("1.2.3.4", ("1.2.3.4", "5.6.7.8")) is True


def test_non_allowlisted_ip_rejected():
    assert ip_allowed("9.9.9.9", ("1.2.3.4",)) is False


def test_non_string_token_rejected():
    assert is_authorized(123, "secret") is False  # type: ignore[arg-type]
    assert is_authorized({"x": 1}, "secret") is False  # type: ignore[arg-type]
