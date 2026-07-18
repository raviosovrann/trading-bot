"""Tests for the in-memory login throttle."""

from __future__ import annotations

import pytest

from tradingbot.service.login_guard import LoginGuard, LoginLocked


class _Clock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_below_threshold_does_not_lock() -> None:
    guard = LoginGuard(max_failures=3, lockout_seconds=60, clock=_Clock())
    guard.record_failure("user:a")
    guard.record_failure("user:a")
    guard.check("user:a")  # 2 < 3, no raise


def test_locks_out_at_threshold() -> None:
    clock = _Clock()
    guard = LoginGuard(max_failures=3, lockout_seconds=60, clock=clock)
    for _ in range(3):
        guard.record_failure("user:a")
    with pytest.raises(LoginLocked) as exc:
        guard.check("user:a")
    assert exc.value.retry_after == 60


def test_lock_expires_after_window() -> None:
    clock = _Clock()
    guard = LoginGuard(max_failures=3, lockout_seconds=60, clock=clock)
    for _ in range(3):
        guard.record_failure("user:a")
    clock.now += 61
    guard.check("user:a")  # window elapsed, no raise


def test_success_clears_failures() -> None:
    guard = LoginGuard(max_failures=3, lockout_seconds=60, clock=_Clock())
    guard.record_failure("user:a")
    guard.record_failure("user:a")
    guard.record_success("user:a")
    guard.record_failure("user:a")
    guard.check("user:a")  # counter reset, 1 < 3


def test_keys_are_tracked_independently() -> None:
    guard = LoginGuard(max_failures=2, lockout_seconds=60, clock=_Clock())
    guard.record_failure("user:a", "ip:1.2.3.4")
    guard.record_failure("user:a", "ip:1.2.3.4")
    with pytest.raises(LoginLocked):
        guard.check("ip:1.2.3.4")  # the IP is locked
    with pytest.raises(LoginLocked):
        guard.check("user:a")  # and so is the username
    guard.check("ip:9.9.9.9")  # an unrelated IP is unaffected
