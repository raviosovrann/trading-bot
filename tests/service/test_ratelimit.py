from __future__ import annotations

import pytest

from tradingbot.service.ratelimit import RateLimiter


@pytest.mark.asyncio
async def test_bucket_paces_after_burst() -> None:
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
