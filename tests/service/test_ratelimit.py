"""Tests for the async rate limiter."""

from __future__ import annotations

import pytest

from tradingbot.service.ratelimit import RateLimiter


@pytest.mark.asyncio
async def test_bucket_paces_after_burst() -> None:
    """Verify that the rate limiter paces after the burst allowance is consumed."""
    now = [0.0]
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    limiter = RateLimiter(
        rate_per_sec=2.0,
        burst=2,
        clock=lambda: now[0],
        sleep=fake_sleep,
    )

    await limiter.acquire()
    await limiter.acquire()
    await limiter.acquire()

    assert slept and abs(slept[-1] - 0.5) < 1e-6


def test_invalid_rate_per_sec_rejected() -> None:
    """A non-positive rate raises ValueError."""
    with pytest.raises(ValueError):
        RateLimiter(rate_per_sec=0.0, burst=1)


def test_invalid_burst_rejected() -> None:
    """A non-positive burst raises ValueError."""
    with pytest.raises(ValueError):
        RateLimiter(rate_per_sec=1.0, burst=0)
