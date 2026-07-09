# WebSocket Streaming Plan (v2 Execution Model)

**Design spec:** [TradingBot-Design-V3.md](../TradingBot-Design-V3.md) — see the
"Execution Model: Event-Driven Streaming (v2)" section for the base architecture
and system-flow diagram. This document is the implementation plan; it does not
re-derive the architecture.

**Status:** Planned (no code written yet)

## 1. Motivation — why event-driven beats polling

The v1 runtime keeps a process alive with `run_forever()`, a
`while True: run_once(); sleep(n)` busy loop that repeatedly *pulls* the latest
closed candle over REST. That works, but it has real costs:

- **Latency vs. load tradeoff.** A short sleep hammers the REST API and burns
  rate limit for mostly-empty polls; a long sleep means signals act on bars that
  are already stale. There is no sleep value that is both cheap and timely.
- **Wasted work.** The overwhelming majority of poll iterations return "no new
  bar" and do nothing but consume an API call and a wakeup.
- **Duplicate/gap handling is implicit.** Dedup lives inline in `run_once()`
  (comparing timestamps), and there is no notion of *missed* bars — if a poll is
  skipped or an interval is missed, that bar is simply never seen.
- **Not how exchanges want to be consumed.** Alpaca and Coinbase both expose
  WebSocket market-data streams that *push* a bar the moment it closes. Polling
  re-implements, badly, what the stream gives for free.

Event-driven streaming inverts control: the process **blocks on a WebSocket**
and wakes only when the exchange pushes a closed bar. The key point — the
process still stays alive indefinitely; it just no longer spins. Same liveness,
far less waste, lower latency, and a natural place to handle reconnects and
gap-fills explicitly.

## 2. The new `StreamingFeed` protocol

Polling's pull-based `CandleFeed` (`warmup_candles` + `latest_closed_candle`)
is replaced, for live use, by a push-based protocol:

```python
class StreamingFeed(Protocol):
    def warmup_candles(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        """REST historical fetch at startup — same as CandleFeed today."""

    def on_bar(self, handler: Callable[[Candle], None]) -> None:
        """Register a callback invoked once per closed bar pushed by the stream."""

    def run(self) -> None:
        """Block on the WebSocket event loop until stop() is called."""

    def stop(self) -> None:
        """Request graceful shutdown of the stream and background thread."""
```

Notes:

- `warmup_candles` is retained verbatim from the pull-based feed — warmup is
  still a one-shot REST fetch at startup. The existing REST feed classes can be
  reused for it.
- `on_bar` registers exactly one strategy-facing handler; the runtime registers
  `process_candle` (see below).
- `run()` blocks the calling (main) thread. The actual WebSocket client runs on
  a **background thread**; bars are handed off to the registered handler in a
  thread-safe way (a lock-guarded queue or a single-consumer channel) so the
  strategy always runs on one consistent thread.
- `stop()` is idempotent and safe to call from a signal handler.

`run_once()` remains available and continues to use the pull-based `CandleFeed`
for cron / single-shot invocations. The two models coexist.

## 3. `process_candle` — the pure core

The body of `run_once()` is extracted into a pure function on the runtime:

```python
def process_candle(self, candle: Candle) -> OrderResult | None:
    # dedup by timestamp -> append to buffer -> trim buffer
    # -> strategy.on_bar(buffer) -> router.route(signal)
```

`process_candle` performs **no I/O**: no feed reads, no sleeps, no network. It
takes a candle and returns an `OrderResult | None`. This makes the decision
logic fully unit-testable with plain candle fixtures, and it is exactly the
callback the streaming feed invokes on each pushed bar. `run_once()` becomes a
thin wrapper: read one candle from the pull feed, hand it to `process_candle`.

`run_forever()` is **removed** — the busy polling loop is retired entirely.

## 4. Phase breakdown

Coinbase is **not deferred** — it is Phase 5 of this same effort.

| Phase | Deliverable | Summary |
|-------|-------------|---------|
| 1 | Extract `process_candle`, remove `run_forever()` | Pull the decision logic out of `run_once()` into a pure `process_candle(candle) -> OrderResult \| None`. `run_once()` becomes a thin wrapper. Delete `run_forever()`. |
| 2 | `AlpacaStreamFeed` | Implement `StreamingFeed` over Alpaca's crypto WebSocket. WS client on a background thread; thread-safe handoff of closed bars to the registered handler. Reuse the existing REST feed for `warmup_candles`. |
| 3 | Event-driven `StreamRuntime` + wiring + graceful shutdown | New `StreamRuntime` warms up once, registers `process_candle` via `on_bar`, then blocks on `run()`. Wire into `__main__`/config (streaming vs. one-shot mode). Handle SIGINT/SIGTERM → `stop()` for clean shutdown. Strategy/router/venues untouched. |
| 4 | Reconnection resilience | Watchdog reconnects on drop with exponential backoff (1s → 60s cap). On reconnect, REST-fill bars missed during the outage before resuming the stream, so the buffer has no gaps. |
| 5 | `CoinbaseStreamFeed` | Implement `StreamingFeed` over Coinbase Advanced Trade's WebSocket, same background-thread + handoff shape as Alpaca. Sandbox caveat from the design spec still applies. |

## 5. Test strategy

All tests remain mock/fake-based with **no live network**, consistent with v1.
A fake streaming feed (a `StreamingFeed` that lets a test push synthetic bars
into the registered handler on demand) is the workhorse for phases 3–5.

### Phase 1 — `process_candle` extraction

| Scenario | Expectation |
|----------|-------------|
| Bar fires handler | Feeding a candle to `process_candle` runs strategy + router and returns the `OrderResult` when a signal fires. |
| Duplicate bar dedup | A candle whose timestamp is `<=` the last buffered candle returns `None` and does not grow the buffer. |
| No-signal bar | A candle that produces no strategy signal returns `None`; buffer still grows. |
| Buffer trim | Buffer never exceeds `max_buffer`; oldest bars are dropped. |
| Purity | `process_candle` performs no feed reads / sleeps / network (verified via a strategy+router with fakes; no feed interaction). |
| `run_once()` wrapper | `run_once()` reads one candle and delegates to `process_candle`; behavior matches the old inline path. |
| `run_forever()` gone | Symbol no longer exists / any lingering references removed. |

### Phase 2 — `AlpacaStreamFeed`

| Scenario | Expectation |
|----------|-------------|
| Warmup | `warmup_candles` returns the expected historical bars (mocked REST client). |
| Bar push → handler | A simulated WS bar message is normalized to a `Candle` and delivered to the registered handler exactly once. |
| Thread-safe handoff | Bars pushed from the background thread are observed on the consumer thread without races (deterministic via a fake WS transport). |
| Only closed bars | In-progress/partial bar updates are ignored; only closed bars reach the handler. |

### Phase 3 — `StreamRuntime` + shutdown

| Scenario | Expectation |
|----------|-------------|
| Warmup once | `StreamRuntime` calls `warmup_candles` exactly once at startup and seeds the buffer. |
| Callback wiring | `process_candle` is registered via `on_bar`; a pushed bar drives strategy → router end to end. |
| End-to-end (fake feed + FakeVenue) | Pushing a scripted bar sequence produces the expected orders on `FakeVenue`. |
| Graceful shutdown | A SIGINT/SIGTERM (or explicit `stop()`) unblocks `run()` and shuts the background thread down cleanly, no orphaned threads. |
| Idempotent stop | Calling `stop()` twice is safe. |

### Phase 4 — reconnection resilience

| Scenario | Expectation |
|----------|-------------|
| Disconnect triggers reconnect | A simulated drop causes the watchdog to attempt reconnection. |
| Exponential backoff | Successive failed reconnects follow 1s → 2s → 4s … capped at 60s (asserted via an injected sleep/clock). |
| Reconnect gap-fill | After reconnect, bars that closed during the outage are REST-filled and delivered in order, with dedup preventing overlap with the live stream. |
| No duplicate on overlap | A bar delivered both by gap-fill and the resumed stream is processed once. |

### Phase 5 — `CoinbaseStreamFeed`

| Scenario | Expectation |
|----------|-------------|
| Warmup | `warmup_candles` returns expected bars (mocked Coinbase REST). |
| Bar push → handler | Simulated Coinbase WS bar → normalized `Candle` → handler once. |
| Parity with Alpaca feed | Same `StreamingFeed` contract: threading, dedup, reconnect behaviors mirror the Alpaca feed. |

### Cross-cutting — end-to-end paper

Optional live smoke (outside the automated suite): `StreamRuntime` against
Alpaca **paper** — warm up, connect to the live WS, receive real closed bars,
route orders visible in the Alpaca paper dashboard. Coinbase is real money /
static sandbox, so no live Coinbase smoke by default.
