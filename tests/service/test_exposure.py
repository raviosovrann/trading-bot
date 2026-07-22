"""Per-bot and global exposure accounting (#110).

The bug this replaces, reproduced from the issue on the code before it:

    two_orders=dry_run,dry_run global_used=120.0
    close_status=closed global_used_after_close=120.0

Three separate faults in two lines. Two 60-notional orders both passed a 100
per-bot cap, because each was compared against the cap individually and
nothing accumulated. Both were dry runs that never reached a venue, yet they
consumed 120 of live exposure. And closing the position released none of it.

Exposure here is attributed per order, keyed by client order id, so it can be
revised as the order's fate becomes known instead of being guessed at
submission time.
"""

from __future__ import annotations

import threading

import pytest

from tradingbot.service.exposure import ExposureTracker


@pytest.fixture
def tracker() -> ExposureTracker:
    return ExposureTracker(global_cap=1_000.0)


class TestCumulativeReservation:
    """The headline bug: repeated small orders must accumulate."""

    def test_a_single_order_within_the_cap_is_admitted(self, tracker) -> None:
        assert tracker.reserve("bot-a", "c1", 60.0, per_bot_cap=100.0) is True
        assert tracker.used("bot-a") == pytest.approx(60.0)

    def test_a_second_order_breaching_the_cap_is_refused(self, tracker) -> None:
        tracker.reserve("bot-a", "c1", 60.0, per_bot_cap=100.0)

        assert tracker.reserve("bot-a", "c2", 60.0, per_bot_cap=100.0) is False
        assert tracker.used("bot-a") == pytest.approx(60.0), "refusal must not charge"

    def test_orders_up_to_the_cap_are_admitted(self, tracker) -> None:
        assert tracker.reserve("bot-a", "c1", 60.0, per_bot_cap=100.0) is True
        assert tracker.reserve("bot-a", "c2", 40.0, per_bot_cap=100.0) is True
        assert tracker.used("bot-a") == pytest.approx(100.0)

    def test_re_reserving_the_same_order_is_admitted_not_double_counted(
        self, tracker
    ) -> None:
        # A retry of the same client order id is the same order, not a second
        # one. It must be admitted -- refusing it would block a legitimate
        # resubmission -- and must replace rather than add.
        tracker.reserve("bot-a", "c1", 60.0, per_bot_cap=100.0)

        assert tracker.reserve("bot-a", "c1", 60.0, per_bot_cap=100.0) is True
        assert tracker.used("bot-a") == pytest.approx(60.0)

    def test_re_reserving_at_a_higher_figure_replaces_the_old_one(
        self, tracker
    ) -> None:
        tracker.reserve("bot-a", "c1", 60.0, per_bot_cap=100.0)
        tracker.reserve("bot-a", "c1", 80.0, per_bot_cap=100.0)

        assert tracker.used("bot-a") == pytest.approx(80.0), "replaced, not summed"


class TestPerBotIsolation:
    def test_bots_have_separate_caps(self, tracker) -> None:
        assert tracker.reserve("bot-a", "c1", 90.0, per_bot_cap=100.0) is True
        assert tracker.reserve("bot-b", "c2", 90.0, per_bot_cap=100.0) is True

    def test_exposure_is_reported_per_bot(self, tracker) -> None:
        tracker.reserve("bot-a", "c1", 90.0, per_bot_cap=100.0)
        tracker.reserve("bot-b", "c2", 30.0, per_bot_cap=100.0)

        assert tracker.used("bot-a") == pytest.approx(90.0)
        assert tracker.used("bot-b") == pytest.approx(30.0)

    def test_the_global_cap_binds_across_bots(self) -> None:
        tracker = ExposureTracker(global_cap=150.0)
        assert tracker.reserve("bot-a", "c1", 90.0, per_bot_cap=100.0) is True

        # Within its own cap, but the shared budget is nearly spent.
        assert tracker.reserve("bot-b", "c2", 90.0, per_bot_cap=100.0) is False
        assert tracker.total() == pytest.approx(90.0)

    def test_an_unknown_bot_has_no_exposure(self, tracker) -> None:
        assert tracker.used("never-traded") == 0.0


class TestSettlement:
    """Attribution is revised as the order's fate becomes known."""

    def test_settling_to_zero_releases_the_reservation(self, tracker) -> None:
        tracker.reserve("bot-a", "c1", 60.0, per_bot_cap=100.0)
        tracker.settle("bot-a", "c1", 0.0)

        assert tracker.used("bot-a") == 0.0
        assert tracker.total() == 0.0

    def test_releasing_frees_the_cap_for_another_order(self, tracker) -> None:
        tracker.reserve("bot-a", "c1", 60.0, per_bot_cap=100.0)
        tracker.settle("bot-a", "c1", 0.0)

        assert tracker.reserve("bot-a", "c2", 90.0, per_bot_cap=100.0) is True

    def test_settling_lower_reflects_a_partial_fill(self, tracker) -> None:
        # Reserved 60, only a third filled and the rest cancelled.
        tracker.reserve("bot-a", "c1", 60.0, per_bot_cap=100.0)
        tracker.settle("bot-a", "c1", 20.0)

        assert tracker.used("bot-a") == pytest.approx(20.0)

    def test_settling_is_idempotent(self, tracker) -> None:
        # Replayed venue events settle the same order repeatedly (#135).
        tracker.reserve("bot-a", "c1", 60.0, per_bot_cap=100.0)
        for _ in range(3):
            tracker.settle("bot-a", "c1", 20.0)

        assert tracker.used("bot-a") == pytest.approx(20.0)

    def test_settling_an_unreserved_order_still_records_it(self, tracker) -> None:
        # After a restart the ledger is replayed but nothing was reserved in
        # this process; the position is real and must be accounted for.
        tracker.settle("bot-a", "c1", 45.0)

        assert tracker.used("bot-a") == pytest.approx(45.0)

    def test_settling_above_the_cap_is_recorded_not_refused(self, tracker) -> None:
        # A fill larger than expected is a fact, not a request. Refusing to
        # record it would understate real risk -- the opposite of the point.
        tracker.reserve("bot-a", "c1", 60.0, per_bot_cap=100.0)
        tracker.settle("bot-a", "c1", 150.0)

        assert tracker.used("bot-a") == pytest.approx(150.0)

    def test_a_negative_settlement_is_floored_at_zero(self, tracker) -> None:
        tracker.reserve("bot-a", "c1", 60.0, per_bot_cap=100.0)
        tracker.settle("bot-a", "c1", -10.0)

        assert tracker.used("bot-a") == 0.0


class TestReleaseAll:
    def test_releasing_a_bot_clears_its_exposure(self, tracker) -> None:
        tracker.reserve("bot-a", "c1", 60.0, per_bot_cap=100.0)
        tracker.reserve("bot-a", "c2", 30.0, per_bot_cap=100.0)
        tracker.release_bot("bot-a")

        assert tracker.used("bot-a") == 0.0

    def test_releasing_one_bot_leaves_the_others(self, tracker) -> None:
        tracker.reserve("bot-a", "c1", 60.0, per_bot_cap=100.0)
        tracker.reserve("bot-b", "c2", 30.0, per_bot_cap=100.0)
        tracker.release_bot("bot-a")

        assert tracker.used("bot-b") == pytest.approx(30.0)
        assert tracker.total() == pytest.approx(30.0)


class TestInvalidInput:
    @pytest.mark.parametrize("notional", [float("nan"), float("inf")])
    def test_an_unusable_notional_is_refused(self, tracker, notional) -> None:
        assert tracker.reserve("bot-a", "c1", notional, per_bot_cap=100.0) is False
        assert tracker.used("bot-a") == 0.0

    def test_a_negative_notional_is_refused(self, tracker) -> None:
        assert tracker.reserve("bot-a", "c1", -5.0, per_bot_cap=100.0) is False

    def test_a_zero_cap_admits_nothing(self, tracker) -> None:
        assert tracker.reserve("bot-a", "c1", 1.0, per_bot_cap=0.0) is False


class TestConcurrency:
    """Two bots submitting at once must not both pass the same budget.

    The check and the update have to be one atomic step. Order placement runs
    on per-bot worker threads (#111), so this is genuine thread concurrency
    rather than interleaved coroutines.
    """

    def test_concurrent_reservations_cannot_exceed_the_global_cap(self) -> None:
        tracker = ExposureTracker(global_cap=100.0)
        admitted: list[bool] = []
        lock = threading.Lock()
        start = threading.Barrier(8)

        def submit(index: int) -> None:
            start.wait()  # release all threads at the same instant
            ok = tracker.reserve(f"bot-{index}", f"c{index}", 20.0, per_bot_cap=100.0)
            with lock:
                admitted.append(ok)

        threads = [threading.Thread(target=submit, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        # 100 / 20 = exactly five may pass, whichever five win the race.
        assert sum(admitted) == 5
        assert tracker.total() == pytest.approx(100.0)

    def test_concurrent_reservations_on_one_bot_respect_its_cap(self) -> None:
        tracker = ExposureTracker(global_cap=10_000.0)
        admitted: list[bool] = []
        lock = threading.Lock()
        start = threading.Barrier(10)

        def submit(index: int) -> None:
            start.wait()
            ok = tracker.reserve("bot-a", f"c{index}", 25.0, per_bot_cap=100.0)
            with lock:
                admitted.append(ok)

        threads = [threading.Thread(target=submit, args=(i,)) for i in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert sum(admitted) == 4
        assert tracker.used("bot-a") == pytest.approx(100.0)
