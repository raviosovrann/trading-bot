"""Tests for paginated, rotating trade history (#122)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tradingbot.service.store import BotStore


def _record(n: int) -> dict[str, object]:
    return {
        "bot_id": "bot-1",
        "action": "buy",
        "status": "filled",
        "ok": True,
        "order_id": f"o{n}",
        "symbol": "BTC/USD",
        "ts": 1_700_000_000 + n,
    }


def _store(tmp_path: Path, **kwargs) -> BotStore:
    return BotStore(tmp_path, **kwargs)


def test_appended_trades_get_a_monotonic_seq(tmp_path: Path) -> None:
    """Verify each stored trade carries a strictly increasing per-bot seq."""
    store = _store(tmp_path)

    for n in range(5):
        store.append_trade("bot-1", _record(n))

    records, _ = store.read_trades("bot-1", limit=10)
    assert [r["seq"] for r in records] == [5, 4, 3, 2, 1], "newest first"


def test_read_trades_is_newest_first_and_pages_backward(tmp_path: Path) -> None:
    """Verify the cursor walks the whole history exactly once, in order."""
    store = _store(tmp_path)
    for n in range(25):
        store.append_trade("bot-1", _record(n))

    seen: list[int] = []
    cursor: int | None = None
    pages = 0
    while True:
        page, cursor = store.read_trades("bot-1", limit=10, before_seq=cursor)
        seen.extend(int(r["seq"]) for r in page)
        pages += 1
        if cursor is None:
            break
        assert pages < 10, "pagination did not terminate"

    assert seen == list(range(25, 0, -1))


def test_read_trades_page_is_stable_across_appends(tmp_path: Path) -> None:
    """Verify writes during paging cannot duplicate or skip a record.

    A cursor keyed on seq only ever walks backward into already-written
    history, so a concurrent append lands ahead of the cursor and is not seen
    twice.
    """
    store = _store(tmp_path)
    for n in range(10):
        store.append_trade("bot-1", _record(n))

    first, cursor = store.read_trades("bot-1", limit=5)
    store.append_trade("bot-1", _record(99))
    second, _ = store.read_trades("bot-1", limit=5, before_seq=cursor)

    ids = [r["seq"] for r in first] + [r["seq"] for r in second]
    assert ids == list(range(10, 0, -1))
    assert len(set(ids)) == len(ids)


def test_history_rotates_at_the_size_threshold(tmp_path: Path) -> None:
    """Verify the active log rolls to an archive instead of growing forever."""
    store = _store(tmp_path, trade_rotate_bytes=400)

    for n in range(50):
        store.append_trade("bot-1", _record(n))

    files = sorted(p.name for p in (tmp_path / "trades").glob("bot-1*.jsonl"))
    assert len(files) > 1, f"expected rotation, got {files}"
    for path in (tmp_path / "trades").glob("bot-1*.jsonl"):
        assert path.stat().st_size < 1200, f"{path.name} grew past the threshold"


def test_rotation_never_deletes_history(tmp_path: Path) -> None:
    """Verify every record survives rotation and is still readable in order."""
    store = _store(tmp_path, trade_rotate_bytes=400)
    for n in range(50):
        store.append_trade("bot-1", _record(n))

    seen: list[int] = []
    cursor: int | None = None
    while True:
        page, cursor = store.read_trades("bot-1", limit=7, before_seq=cursor)
        seen.extend(int(r["seq"]) for r in page)
        if cursor is None:
            break

    assert seen == list(range(50, 0, -1))


def test_archives_are_not_renamed_by_later_rotations(tmp_path: Path) -> None:
    """Verify archive filenames are stable, so cursors never dangle."""
    store = _store(tmp_path, trade_rotate_bytes=400)
    for n in range(20):
        store.append_trade("bot-1", _record(n))
    before = sorted(p.name for p in (tmp_path / "trades").glob("bot-1*.jsonl"))

    for n in range(20, 60):
        store.append_trade("bot-1", _record(n))
    after = sorted(p.name for p in (tmp_path / "trades").glob("bot-1*.jsonl"))

    assert before == after[: len(before)], "an existing archive was renamed"


def test_reading_does_not_load_the_whole_history(tmp_path: Path) -> None:
    """Verify a small page touches only the newest archive, not everything."""
    store = _store(tmp_path, trade_rotate_bytes=400)
    for n in range(200):
        store.append_trade("bot-1", _record(n))

    opened: list[str] = []
    real_open = Path.open

    def tracking_open(self: Path, *args, **kwargs):
        opened.append(self.name)
        return real_open(self, *args, **kwargs)

    Path.open = tracking_open  # type: ignore[method-assign]
    try:
        page, _ = store.read_trades("bot-1", limit=3)
    finally:
        Path.open = real_open  # type: ignore[method-assign]

    assert len(page) == 3
    trade_files = [name for name in opened if name.startswith("bot-1")]
    assert len(trade_files) <= 2, f"read fanned out over {trade_files}"


def test_legacy_records_without_seq_are_still_readable(tmp_path: Path) -> None:
    """Verify a pre-#122 log written without seq still paginates."""
    trades = tmp_path / "trades"
    trades.mkdir(parents=True, exist_ok=True)
    legacy = trades / "bot-1.jsonl"
    with legacy.open("w", encoding="utf-8") as f:
        for n in range(3):
            record = _record(n)
            record.pop("seq", None)
            f.write(json.dumps(record) + "\n")

    store = _store(tmp_path)
    page, _ = store.read_trades("bot-1", limit=10)

    assert [r["order_id"] for r in page] == ["o2", "o1", "o0"]
    assert [r["seq"] for r in page] == [3, 2, 1]


def test_appending_after_legacy_records_continues_the_sequence(tmp_path: Path) -> None:
    """Verify new trades do not reuse a seq already taken by legacy rows."""
    trades = tmp_path / "trades"
    trades.mkdir(parents=True, exist_ok=True)
    with (trades / "bot-1.jsonl").open("w", encoding="utf-8") as f:
        for n in range(3):
            f.write(json.dumps(_record(n)) + "\n")

    store = _store(tmp_path)
    store.append_trade("bot-1", _record(9))

    page, _ = store.read_trades("bot-1", limit=10)
    seqs = [int(r["seq"]) for r in page]
    assert seqs == [4, 3, 2, 1]
    assert len(set(seqs)) == 4


def test_read_trades_rejects_an_unsafe_bot_id(tmp_path: Path) -> None:
    """Verify path traversal is still refused on the paginated reader."""
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        store.read_trades("../escape", limit=5)


def test_empty_history_returns_no_cursor(tmp_path: Path) -> None:
    """Verify an unknown bot pages cleanly rather than raising."""
    store = _store(tmp_path)
    page, cursor = store.read_trades("bot-1", limit=10)
    assert page == []
    assert cursor is None
