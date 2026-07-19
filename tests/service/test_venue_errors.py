"""Venue errors must reach the operator readably and without secrets (#175)."""

from __future__ import annotations

import pytest

from tradingbot.service.venue_errors import classify_venue_error, redact

ccxt = pytest.importorskip("ccxt")


class TestRedaction:
    """ccxt embeds the signed request URL in its messages — including the key."""

    def test_a_signed_request_url_loses_its_query_string(self) -> None:
        """The real message that prompted this issue must not leak its signature."""
        message = (
            "binance GET https://api.binance.com/sapi/v1/capital/config/getall"
            "?timestamp=1784480879223&recvWindow=10000"
            "&signature=f7adcc27fe7a49c2854d708dece4ebd801ce3b9e9d8ba1012310f5708f183059"
            ' 451 {"code": 0, "msg": "Service unavailable from a restricted location"}'
        )

        safe = redact(message)

        assert "signature=" not in safe
        assert "f7adcc27" not in safe
        assert "1784480879223" not in safe
        # The useful part survives.
        assert "restricted location" in safe
        assert "api.binance.com/sapi/v1/capital/config/getall" in safe

    @pytest.mark.parametrize(
        "secret",
        [
            "apiKey=AKIAIOSFODNN7EXAMPLE",
            "api_key=super-secret-value",
            "secret=hunter2hunter2",
            "signature=deadbeefcafe",
            "password=letmein",
            "token=ey.JhbGciOi.JIUzI1",
            "access_token=abc123def456",
        ],
    )
    def test_credential_bearing_parameters_are_scrubbed(self, secret: str) -> None:
        """Verify sensitive key=value pairs are removed wherever they appear."""
        value = secret.split("=", 1)[1]

        safe = redact(f"venue rejected the request ({secret}) and gave up")

        assert value not in safe
        assert "venue rejected the request" in safe

    def test_a_very_long_message_is_truncated(self) -> None:
        """Verify a huge upstream body cannot be echoed wholesale."""
        safe = redact("x" * 5000)
        assert len(safe) < 1000

    def test_an_ordinary_message_is_left_alone(self) -> None:
        """Verify redaction does not mangle a harmless message."""
        assert redact("exchange is under maintenance") == "exchange is under maintenance"


class TestClassification:
    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            (ccxt.AuthenticationError("bad key"), 400),
            (ccxt.PermissionDenied("not allowed"), 400),
            (ccxt.BadSymbol("no such market"), 400),
            (ccxt.NotSupported("no such feature"), 400),
            (ccxt.RateLimitExceeded("slow down"), 429),
            (ccxt.ExchangeNotAvailable("try later"), 502),
            (ccxt.OnMaintenance("scheduled downtime"), 502),
            (ccxt.RequestTimeout("timed out"), 502),
            (ccxt.NetworkError("connection reset"), 502),
            (ccxt.RestrictedLocation("wrong country"), 502),
        ],
    )
    def test_known_venue_errors_map_to_a_useful_status(self, error, expected: int) -> None:
        """Verify each failure kind gets a status that tells the operator whose fault it is."""
        result = classify_venue_error(error)
        assert result is not None
        assert result[0] == expected

    def test_rate_limit_beats_its_network_error_base(self) -> None:
        """RateLimitExceeded subclasses NetworkError, so order of checks matters."""
        result = classify_venue_error(ccxt.RateLimitExceeded("slow down"))
        assert result is not None
        assert result[0] == 429, "matched the NetworkError base instead of the subclass"

    def test_an_unrecognised_ccxt_error_is_still_treated_as_upstream(self) -> None:
        """A ccxt exception is by definition a venue interaction, not our bug."""
        result = classify_venue_error(ccxt.ExchangeError("something odd"))
        assert result is not None
        assert result[0] == 502

    def test_a_non_venue_exception_is_not_disguised(self) -> None:
        """Our own bugs must keep returning 500 rather than blaming the venue."""
        assert classify_venue_error(TypeError("bug in our code")) is None
        assert classify_venue_error(KeyError("missing")) is None
        assert classify_venue_error(ValueError("bad input")) is None

    def test_the_detail_names_the_error_kind_and_is_redacted(self) -> None:
        """Verify the message is both useful and safe."""
        error = ccxt.AuthenticationError(
            "coinbase GET https://api.coinbase.com/v2/accounts?api_key=leak-me 401"
        )

        result = classify_venue_error(error)
        assert result is not None
        status, detail = result

        assert status == 400
        assert "AuthenticationError" in detail
        assert "leak-me" not in detail

    def test_an_empty_message_still_produces_a_detail(self) -> None:
        """Verify a bare exception does not yield an empty explanation."""
        result = classify_venue_error(ccxt.ExchangeNotAvailable())
        assert result is not None
        assert "ExchangeNotAvailable" in result[1]
