"""Tests for bounded off-loop execution of synchronous venue calls (#111)."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from tradingbot.service.blocking import BlockingCallTimeout, BlockingCalls, WorkerPools


@pytest.mark.asyncio
async def test_run_returns_the_callables_result() -> None:
    """Verify a synchronous call is executed and its value returned."""
    workers = BlockingCalls("test")
    try:
        assert await workers.run(lambda a, b: a + b, 2, 3) == 5
    finally:
        workers.shutdown()


@pytest.mark.asyncio
async def test_run_executes_off_the_event_loop_thread() -> None:
    """Verify the work does not run on the loop thread."""
    workers = BlockingCalls("test")
    loop_thread = threading.get_ident()
    try:
        worker_thread = await workers.run(threading.get_ident)
    finally:
        workers.shutdown()
    assert worker_thread != loop_thread


@pytest.mark.asyncio
async def test_the_loop_stays_responsive_while_a_call_blocks() -> None:
    """Verify a blocking call cannot freeze other loop work.

    This is the whole point of the issue: one slow exchange must not stop the
    API from serving.
    """
    workers = BlockingCalls("test")
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        for _ in range(10):
            await asyncio.sleep(0.01)
            ticks += 1

    try:
        tick_task = asyncio.create_task(ticker())
        await workers.run(time.sleep, 0.2)
        await tick_task
    finally:
        workers.shutdown()

    assert ticks == 10, "the event loop was blocked by the synchronous call"


@pytest.mark.asyncio
async def test_a_call_that_overruns_its_timeout_is_abandoned() -> None:
    """Verify a hung exchange call does not hang its caller forever."""
    workers = BlockingCalls("test", timeout=0.05)
    try:
        with pytest.raises(BlockingCallTimeout):
            await workers.run(time.sleep, 5.0)
    finally:
        workers.shutdown(wait=False)


@pytest.mark.asyncio
async def test_exceptions_propagate_to_the_caller() -> None:
    """Verify a venue error surfaces rather than being swallowed."""
    workers = BlockingCalls("test")

    def boom() -> None:
        raise ValueError("venue rejected the order")

    try:
        with pytest.raises(ValueError, match="rejected"):
            await workers.run(boom)
    finally:
        workers.shutdown()


@pytest.mark.asyncio
async def test_pool_is_bounded() -> None:
    """Verify concurrency is capped, so a venue cannot spawn unbounded threads."""
    workers = BlockingCalls("test", max_workers=2)
    active = 0
    peak = 0
    guard = threading.Lock()

    def work() -> None:
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with guard:
            active -= 1

    try:
        await asyncio.gather(*(workers.run(work) for _ in range(8)))
    finally:
        workers.shutdown()

    assert peak <= 2


@pytest.mark.asyncio
async def test_one_slow_pool_does_not_starve_another() -> None:
    """Verify per-venue isolation: a stuck venue only exhausts its own workers."""
    pools = WorkerPools(max_workers=1, timeout=5.0)
    slow = pools.for_name("slow-venue")
    fast = pools.for_name("fast-venue")

    try:
        stuck = asyncio.create_task(slow.run(time.sleep, 1.0))
        await asyncio.sleep(0.05)  # let it occupy the slow pool's only worker

        started = time.monotonic()
        assert await asyncio.wait_for(fast.run(lambda: "served"), timeout=0.5) == "served"
        assert time.monotonic() - started < 0.5

        stuck.cancel()
        await asyncio.gather(stuck, return_exceptions=True)
    finally:
        pools.shutdown()


def test_worker_pools_reuses_a_pool_per_name() -> None:
    """Verify pools are shared per venue rather than created per call."""
    pools = WorkerPools()
    try:
        assert pools.for_name("coinbase") is pools.for_name("coinbase")
        assert pools.for_name("coinbase") is not pools.for_name("kraken")
    finally:
        pools.shutdown()


def test_rejects_a_non_positive_bound() -> None:
    """Verify a pool that could never run anything is refused."""
    with pytest.raises(ValueError):
        BlockingCalls("test", max_workers=0)
    with pytest.raises(ValueError):
        BlockingCalls("test", timeout=0)
