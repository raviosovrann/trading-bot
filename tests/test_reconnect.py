from tradingbot.stream import run_with_reconnect


def test_backoff_schedule_1_2_4_capped_at_60():
    sleeps = []

    def connect():
        raise ConnectionError("down")

    def should_stop():
        return len(sleeps) >= 8

    run_with_reconnect(
        connect_and_run=connect,
        should_stop=should_stop,
        sleep=sleeps.append,
    )
    assert sleeps == [1, 2, 4, 8, 16, 32, 60, 60]


def test_backoff_resets_after_healthy_connection():
    sleeps = []
    state = {"n": 0}

    def connect():
        state["n"] += 1
        if state["n"] == 1:
            return  # healthy connection that then drops
        raise ConnectionError("down")

    def should_stop():
        return len(sleeps) >= 3

    run_with_reconnect(
        connect_and_run=connect,
        should_stop=should_stop,
        sleep=sleeps.append,
    )
    assert sleeps == [1, 1, 2]


def test_stops_immediately_when_should_stop_true():
    ran = []
    sleeps = []

    run_with_reconnect(
        connect_and_run=lambda: ran.append(1),
        should_stop=lambda: True,
        sleep=sleeps.append,
    )
    assert ran == []
    assert sleeps == []


def test_gap_fill_called_after_disconnect():
    sleeps = []
    gaps = []

    def should_stop():
        return len(sleeps) >= 2

    run_with_reconnect(
        connect_and_run=lambda: None,
        should_stop=should_stop,
        gap_fill=lambda: gaps.append(1),
        sleep=sleeps.append,
    )
    assert len(gaps) >= 1
